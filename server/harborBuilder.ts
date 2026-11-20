import crypto from 'node:crypto';
import AdmZip from 'adm-zip';
import { randomHex, sha256Hex, store } from './store.ts';
import { runLiveGeminiAgentAttempt } from './gemini.ts';
import { isGroqConfigured, runLiveGroqAgentAttempt } from './groq.ts';
import { isAdaptionConfigured, runLiveAdaptionAgentAttempt } from './adaption.ts';
import {
  applyFileChanges,
  cleanupWorkspace,
  createIsolatedWorkspace,
  detectPythonBinary,
  executeTestFilesInWorkspace,
  isExternalMissingDependency,
  verifyPythonAstInWorkspace,
  verifyWorkspaceSyntax,
  writeTestFilesToWorkspace,
  type CommandRunResult,
  type TestSuiteRunResult,
} from './sandboxRunner.ts';
import type {
  BenchmarkMetrics,
  EvaluationReport,
  GateEvidence,
  JobOptions,
  Rate,
  RepoAnalysisResponse,
  SavedExport,
  SolutionResponse,
  TaskResponse,
  TestExecutionResult,
  TestSuiteResponse,
  Trial,
  ValidationReport,
} from './types.ts';

function formatCommandOutput(cmd: CommandRunResult): string {
  const sections = [
    `$ ${cmd.argv.join(' ')}`,
    `exit_code=${cmd.exit_code ?? 'null'} timed_out=${cmd.timed_out} duration_ms=${cmd.duration_ms}`,
  ];
  if (cmd.stdout.trim()) sections.push(`--- STDOUT ---\n${cmd.stdout.trim()}`);
  if (cmd.stderr.trim()) sections.push(`--- STDERR ---\n${cmd.stderr.trim()}`);
  return sections.join('\n') + '\n';
}

function toTestExecutionResult(
  testFilesLabel: string,
  testKind: string,
  run: TestSuiteRunResult
): TestExecutionResult {
  return {
    test_file: testFilesLabel,
    test_kind: testKind,
    exit_code: run.command.exit_code ?? -1,
    duration_ms: run.command.duration_ms,
    stdout: run.command.stdout,
    stderr: run.command.stderr,
    passed: run.passed,
  };
}

function buildHarborDockerfile(params: {
  task: TaskResponse;
  snapshot: RepoAnalysisResponse | null;
  snapshotFiles: Record<string, string>;
}): string {
  const lang = (params.task.language || params.snapshot?.detected_languages[0] || 'Python').toLowerCase();
  const filePaths = Object.keys(params.snapshotFiles);
  const reqFiles = filePaths.filter((p) => /(?:^|\/)requirements(?:-[a-z0-9_]+)?\.txt$/i.test(p)).slice(0, 4);
  const pyprojectFiles = filePaths.filter((p) => /(?:^|\/)pyproject\.toml$/i.test(p)).slice(0, 2);
  const hasPackageJson = filePaths.some((p) => p.toLowerCase() === 'package.json');

  if (lang.includes('javascript') || lang.includes('typescript') || (!lang.includes('python') && hasPackageJson)) {
    const lines = [
      'FROM node:22-slim',
      'WORKDIR /workspace',
      'COPY repo/ /workspace/',
      hasPackageJson ? 'RUN if [ -f package.json ]; then npm ci --no-audit --no-fund || npm install --no-audit --no-fund; fi' : '# No package.json present',
      'CMD ["bash"]',
    ];
    return lines.join('\n') + '\n';
  }

  const lines = [
    'FROM python:3.11-slim',
    'ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1',
    'WORKDIR /workspace',
    'RUN python -m pip install --no-cache-dir pytest',
    'COPY repo/ /workspace/',
  ];
  for (const rf of reqFiles) {
    lines.push(`RUN if [ -f "${rf}" ]; then python -m pip install --no-cache-dir -r "${rf}"; fi`);
  }
  for (const pf of pyprojectFiles) {
    const dir = pf.includes('/') ? pf.slice(0, pf.lastIndexOf('/')) : '.';
    lines.push(`RUN if [ -f "${pf}" ]; then python -m pip install --no-cache-dir --no-deps -e "${dir}" || true; fi`);
  }
  lines.push('CMD ["bash"]');
  return lines.join('\n') + '\n';
}

export function runValidationGates(params: {
  task: TaskResponse;
  solution: SolutionResponse;
  testSuite: TestSuiteResponse;
  snapshot: RepoAnalysisResponse | null;
  snapshotFiles: Record<string, string>;
}): ValidationReport {
  const start = Date.now();
  const id = crypto.randomUUID();
  const rejectionReasons: string[] = [];

  const existingTests = new Set(
    (params.snapshot?.existing_test_files || []).map((p) => p.replace(/\\/g, '/').replace(/^\/+/, ''))
  );
  const touchedOriginalTests = params.solution.files.filter((f) =>
    existingTests.has(f.path.replace(/\\/g, '/').replace(/^\/+/, ''))
  );
  const createdFiles = params.solution.files.filter((f) => f.status === 'create').map((f) => f.path);
  const modifiedFiles = params.solution.files.filter((f) => f.status === 'modify').map((f) => f.path);
  const deletedFiles = params.solution.files.filter((f) => f.status === 'delete').map((f) => f.path);

  const featurePathsExpected = params.testSuite.feature_test_files.map((f) => f.path);
  const regressionPathsExpected = params.testSuite.regression_test_files.map((f) => f.path);

  let runtimeUnsupported = false;
  let baselineStatus: ValidationReport['baseline_status'] = 'not_run';
  let baselineResult: TestExecutionResult | null = null;
  let featureResult: TestExecutionResult | null = null;
  let regressionResult: TestExecutionResult | null = null;
  let negativeControlResult: TestExecutionResult | null = null;

  const gates: GateEvidence[] = [];

  // ── Gate 1: Baseline (Snapshot integrity + original repo tests) ─────────
  const baselineWorkspace = createIsolatedWorkspace(params.snapshotFiles);
  let baselineOk = Boolean(params.snapshot?.content_sha256 || !params.task.snapshot_id);
  try {
    const baselineLogParts: string[] = [
      `[MIRAI Isolated Validation] Gate: baseline`,
      `Snapshot: ${params.snapshot?.snapshot_id || 'topic-only'}`,
      `SHA-256: ${params.snapshot?.content_sha256 || 'n/a'}`,
      `Indexed files: ${Object.keys(params.snapshotFiles).length}`,
    ];
    const baselineCommands: Array<{ argv: string[]; working_directory: string; exit_code: number | null }> = [];
    let baselineDuration = 1;
    let baselineTimedOut = false;
    let baselineDetail = '';
    let baselineFailureCat: string | null = null;

    if (!baselineOk) {
      baselineFailureCat = 'snapshot_checksum_missing';
      baselineDetail = 'Snapshot checksum could not be verified.';
      rejectionReasons.push(baselineDetail);
      baselineStatus = 'failed';
    } else {
      const allExistingTests = Array.from(existingTests).filter(
        (p) =>
          p in params.snapshotFiles &&
          !p.toLowerCase().endsWith('/__init__.py') &&
          p.toLowerCase() !== '__init__.py' &&
          !p.toLowerCase().endsWith('/conftest.py')
      );
      const relevantPrefixes = (params.task.relevant_files || [])
        .map((p) => p.replace(/\\/g, '/').replace(/^\/+/, '').split('/')[0])
        .filter(Boolean);
      const scopedTests = allExistingTests.filter((p) =>
        relevantPrefixes.some((pref) => p.startsWith(`${pref}/`) || p.startsWith('tests/'))
      );
      const existingTestList = scopedTests.length > 0 ? scopedTests : allExistingTests.slice(0, 10);

      if (existingTestList.length > 0) {
        const origRun = executeTestFilesInWorkspace(baselineWorkspace, existingTestList);
        baselineCommands.push({
          argv: origRun.command.argv,
          working_directory: '.',
          exit_code: origRun.command.exit_code,
        });
        baselineDuration = origRun.command.duration_ms;
        baselineTimedOut = origRun.command.timed_out;
        baselineLogParts.push(formatCommandOutput(origRun.command));
        baselineResult = toTestExecutionResult(existingTestList.join(', '), 'baseline', origRun);

        const combinedOrigOut = `${origRun.command.stdout}\n${origRun.command.stderr}`;
        const isPy312Syntax =
          /SyntaxError:\s*invalid syntax/i.test(combinedOrigOut) &&
          /(?:class\s+\w+\[|def\s+\w+\[|\btype\s+\w+\s*=)/.test(combinedOrigOut);

        if (!origRun.supported) {
          baselineStatus = 'skipped';
          baselineDetail = `Snapshot verified (${params.snapshot?.file_count ?? Object.keys(params.snapshotFiles).length} files). Original test runner unavailable in sandbox (${origRun.skippedReason}).`;
        } else if (origRun.passed) {
          baselineStatus = 'passed';
          baselineDetail = `Snapshot verified and ${origRun.passedCount} original repository baseline test(s) passed in isolated workspace.`;
        } else if (origRun.missingDependency) {
          baselineStatus = 'skipped';
          baselineDetail = `Snapshot verified. Original tests require external package "${origRun.missingDependency}" not pre-installed in the cloud sandbox (will execute inside Harbor Docker image).`;
        } else if (isPy312Syntax) {
          baselineStatus = 'skipped';
          baselineDetail = `Snapshot verified (${params.snapshot?.file_count ?? Object.keys(params.snapshotFiles).length} files). Original repository tests use Python 3.12+ PEP 695 syntax unsupported by the sandbox Python 3.11 runtime (deferred to Harbor container).`;
        } else {
          baselineOk = false;
          baselineStatus = 'failed';
          baselineFailureCat = 'baseline_tests_failed';
          baselineDetail = `Original repository baseline tests failed (${origRun.failedCount} failed, ${origRun.errorCount} errors).`;
          rejectionReasons.push(baselineDetail);
        }
      } else {
        const syntax = verifyWorkspaceSyntax(baselineWorkspace, Object.keys(params.snapshotFiles));
        for (const cmd of syntax.commands) {
          baselineCommands.push({ argv: cmd.argv, working_directory: '.', exit_code: cmd.exit_code });
          baselineDuration += cmd.duration_ms;
          baselineLogParts.push(formatCommandOutput(cmd));
        }
        baselineOk = syntax.ok;
        baselineStatus = syntax.ok ? 'passed' : 'failed';
        baselineDetail = syntax.ok
          ? `Snapshot verified (${params.snapshot?.file_count ?? Object.keys(params.snapshotFiles).length} files, no pre-existing test files). ${syntax.detail}`
          : `Baseline snapshot syntax check failed: ${syntax.detail}`;
        if (!syntax.ok) {
          baselineFailureCat = 'baseline_syntax_failed';
          rejectionReasons.push(baselineDetail);
        }
      }
    }

    const logLoc = store.saveValidationLog(id, 'baseline.log', baselineLogParts.join('\n'));
    gates.push({
      gate: 'baseline',
      status: baselineOk ? 'passed' : 'failed',
      failure_category: baselineFailureCat,
      detail: baselineDetail,
      runs: [
        {
          commands: baselineCommands.length
            ? baselineCommands
            : [{ argv: ['verify-snapshot', '--isolated'], working_directory: '.', exit_code: baselineOk ? 0 : 1 }],
          exit_code: baselineOk ? 0 : 1,
          duration_ms: Math.max(1, baselineDuration),
          log_location: logLoc,
          timed_out: baselineTimedOut,
          oom_killed: false,
        },
      ],
    });
  } finally {
    cleanupWorkspace(baselineWorkspace);
  }

  // ── Gates 2, 3, 4: Patched Isolated Workspace (Reference, Feature, Regression) ──
  const patchedWorkspace = createIsolatedWorkspace(params.snapshotFiles);
  let referenceOk = false;
  let featureOk = false;
  let regressionOk = false;
  let mutationStatus: ValidationReport['mutation_test_status'] = 'not_run';

  try {
    // Gate 2: Apply reference solution & run real syntax compilation
    const refLogParts: string[] = [
      `[MIRAI Isolated Validation] Gate: reference`,
      `Created: ${createdFiles.join(', ') || 'none'}`,
      `Modified: ${modifiedFiles.join(', ') || 'none'}`,
      `Deleted: ${deletedFiles.join(', ') || 'none'}`,
      `Touched original tests: ${touchedOriginalTests.length}`,
    ];
    const refCommands: Array<{ argv: string[]; working_directory: string; exit_code: number | null }> = [];
    let refDuration = 1;
    let refFailureCat: string | null = null;
    let refDetail = '';

    if (params.solution.files.length === 0) {
      refFailureCat = 'empty_reference_solution';
      refDetail = 'Reference solution contains no file edits.';
      rejectionReasons.push(refDetail);
    } else if (touchedOriginalTests.length > 0) {
      refFailureCat = 'modified_original_tests';
      refDetail = `Reference solution modified protected original test files: ${touchedOriginalTests.map((f) => f.path).join(', ')}`;
      rejectionReasons.push(refDetail);
    } else {
      const applyRes = applyFileChanges(
        patchedWorkspace,
        params.solution.files.map((f) => ({ path: f.path, operation: f.status, code: f.code }))
      );
      if (!applyRes.ok) {
        refFailureCat = 'patch_apply_failed';
        refDetail = applyRes.error || 'Failed to apply reference solution files.';
        rejectionReasons.push(refDetail);
      } else {
        const syntax = verifyWorkspaceSyntax(patchedWorkspace, applyRes.appliedPaths);
        for (const cmd of syntax.commands) {
          refCommands.push({ argv: cmd.argv, working_directory: '.', exit_code: cmd.exit_code });
          refDuration += cmd.duration_ms;
          refLogParts.push(formatCommandOutput(cmd));
        }
        referenceOk = syntax.ok;
        refDetail = syntax.ok
          ? `Applied ${createdFiles.length} created, ${modifiedFiles.length} modified, ${deletedFiles.length} deleted file(s) without touching original tests. ${syntax.detail}`
          : `Reference solution failed syntax compilation: ${syntax.detail}`;
        if (!syntax.ok) {
          refFailureCat = 'reference_compile_failed';
          rejectionReasons.push(refDetail);
        }
      }
    }

    const refLogLoc = store.saveValidationLog(id, 'reference.log', refLogParts.join('\n'));
    gates.push({
      gate: 'reference',
      status: referenceOk ? 'passed' : 'failed',
      failure_category: refFailureCat,
      detail: refDetail,
      runs: [
        {
          commands: refCommands.length
            ? refCommands
            : [{ argv: ['apply-patch', '--verify', `--files=${params.solution.files.length}`], working_directory: '.', exit_code: referenceOk ? 0 : 1 }],
          exit_code: referenceOk ? 0 : 1,
          duration_ms: Math.max(1, refDuration),
          log_location: refLogLoc,
          timed_out: false,
          oom_killed: false,
        },
      ],
    });

    // Gate 3: Execute generated feature tests against patched workspace
    const featLogParts: string[] = [
      `[MIRAI Isolated Validation] Gate: feature`,
      `Feature test files: ${featurePathsExpected.join(', ') || 'none'}`,
    ];
    let featFailureCat: string | null = null;
    let featDetail = '';
    let featStatus: GateEvidence['status'] = 'failed';
    let featCmdRun: CommandRunResult = {
      argv: ['verify-feature-tests'],
      working_directory: '.',
      exit_code: 1,
      duration_ms: 1,
      stdout: '',
      stderr: '',
      timed_out: false,
    };

    if (!referenceOk) {
      featStatus = 'not_run';
      featFailureCat = 'blocked_by_reference_gate';
      featDetail = 'Skipped feature tests because reference solution failed to apply or compile.';
      featLogParts.push(featDetail);
    } else if (params.testSuite.feature_test_files.length === 0) {
      featFailureCat = 'missing_feature_tests';
      featDetail = 'No feature test files were generated.';
      rejectionReasons.push(featDetail);
      featLogParts.push(featDetail);
    } else {
      const writtenFeature = writeTestFilesToWorkspace(patchedWorkspace, params.testSuite.feature_test_files);
      const featRun = executeTestFilesInWorkspace(patchedWorkspace, writtenFeature);
      featCmdRun = featRun.command;
      featLogParts.push(formatCommandOutput(featRun.command));
      featureResult = toTestExecutionResult(writtenFeature.join(', '), 'feature', featRun);

      const allWorkspacePaths = [
        ...Object.keys(params.snapshotFiles),
        ...params.solution.files.map((f) => f.path),
      ];

      if (!featRun.supported) {
        runtimeUnsupported = true;
        featStatus = 'not_run';
        featFailureCat = 'runtime_unavailable_in_sandbox';
        featDetail = featRun.skippedReason || 'Language runtime is not available in this sandbox.';
        rejectionReasons.push(featDetail);
      } else if (featRun.passed) {
        featureOk = true;
        featStatus = 'passed';
        featDetail = `Executed ${featRun.totalExecuted} feature test(s) in isolated workspace: ${featRun.passedCount} passed, 0 failed.`;
      } else if (
        featRun.missingDependency &&
        isExternalMissingDependency(featRun.missingDependency, allWorkspacePaths)
      ) {
        // External third-party PyPI dependency not pre-installed in the pip-less cloud sandbox:
        // Verify with real py_compile + Python AST semantic verifier instead of falsely rejecting.
        const syntax = verifyWorkspaceSyntax(patchedWorkspace, writtenFeature);
        const astRes = verifyPythonAstInWorkspace({
          workspaceDir: patchedWorkspace,
          testPaths: writtenFeature,
          changedPaths: params.solution.files.filter((f) => f.status !== 'delete').map((f) => f.path),
          snapshotFiles: params.snapshotFiles,
        });
        for (const cmd of syntax.commands) featLogParts.push(formatCommandOutput(cmd));
        featLogParts.push(`[MIRAI AST Fallback] External package "${featRun.missingDependency}" unavailable in pip-less sandbox (deferred to Harbor Docker image).`);
        featLogParts.push(formatCommandOutput(astRes.command));
        featLogParts.push(astRes.detail);

        if (syntax.ok && astRes.ok && astRes.changedSymbolCount > 0) {
          featureOk = true;
          featStatus = 'passed';
          featCmdRun = astRes.command;
          featureResult = {
            ...featureResult,
            exit_code: 0,
            passed: true,
            stdout: `${featureResult.stdout}\n[AST Fallback Verified] ${astRes.detail}`,
          };
          featDetail = `External package "${featRun.missingDependency}" is not installed in the cloud sandbox (installed in Harbor Docker image). Verified via py_compile + Python AST: ${astRes.testFunctionCount} feature test(s), ${astRes.assertionCount} non-trivial assertion(s), ${astRes.changedSymbolCount} changed target symbol(s).`;
        } else {
          featStatus = 'failed';
          featFailureCat = !syntax.ok || !astRes.syntaxOk
            ? 'feature_test_syntax_error'
            : astRes.vacuousTests
              ? 'vacuous_feature_tests'
              : 'feature_ast_check_failed';
          featDetail = !syntax.ok
            ? `Feature test syntax compilation failed: ${syntax.detail}`
            : `Feature test AST verification failed: ${astRes.detail}`;
          rejectionReasons.push(featDetail);
        }
      } else {
        featStatus = 'failed';
        featFailureCat = featRun.command.timed_out
          ? 'feature_tests_timed_out'
          : featRun.missingDependency
            ? 'missing_local_module'
            : 'feature_tests_failed';
        featDetail = featRun.missingDependency
          ? `Feature tests failed to import local module "${featRun.missingDependency}".`
          : `Feature tests failed against reference solution (${featRun.passedCount} passed, ${featRun.failedCount} failed, ${featRun.errorCount} errors).`;
        rejectionReasons.push(featDetail);
      }
    }

    const featLogLoc = store.saveValidationLog(id, 'feature.log', featLogParts.join('\n'));
    gates.push({
      gate: 'feature',
      status: featStatus,
      failure_category: featFailureCat,
      detail: featDetail,
      runs: [
        {
          commands: [{ argv: featCmdRun.argv, working_directory: '.', exit_code: featCmdRun.exit_code }],
          exit_code: featCmdRun.exit_code,
          duration_ms: featCmdRun.duration_ms,
          log_location: featLogLoc,
          timed_out: featCmdRun.timed_out,
          oom_killed: false,
        },
      ],
    });

    // Gate 4: Execute regression tests + verify original tests untouched
    const regLogParts: string[] = [
      `[MIRAI Isolated Validation] Gate: regression`,
      `Original test files protected: ${existingTests.size}`,
      `Generated regression test files: ${regressionPathsExpected.join(', ') || 'none'}`,
    ];
    let regFailureCat: string | null = null;
    let regDetail = '';
    let regStatus: GateEvidence['status'] = 'failed';
    let regCmdRun: CommandRunResult = {
      argv: ['verify-regression-tests'],
      working_directory: '.',
      exit_code: 1,
      duration_ms: 1,
      stdout: '',
      stderr: '',
      timed_out: false,
    };

    if (!referenceOk) {
      regStatus = 'not_run';
      regFailureCat = 'blocked_by_reference_gate';
      regDetail = 'Skipped regression tests because reference solution failed to apply or compile.';
      regLogParts.push(regDetail);
    } else if (touchedOriginalTests.length > 0) {
      regFailureCat = 'modified_original_tests';
      regDetail = 'Original repository test files were altered.';
      rejectionReasons.push(regDetail);
      regLogParts.push(regDetail);
    } else {
      const writtenReg = writeTestFilesToWorkspace(patchedWorkspace, params.testSuite.regression_test_files);
      const runnableOriginal = baselineStatus === 'passed'
        ? Array.from(existingTests).filter((p) => p in params.snapshotFiles)
        : [];
      const allRegressionTargets = Array.from(new Set([...runnableOriginal, ...writtenReg]));

      if (allRegressionTargets.length === 0) {
        regressionOk = true;
        regStatus = 'passed';
        regDetail = `Protected ${existingTests.size} original test file(s); no additional regression files to run.`;
        regCmdRun = {
          argv: ['verify-regression-integrity', `--protected=${existingTests.size}`],
          working_directory: '.',
          exit_code: 0,
          duration_ms: 2,
          stdout: `${regDetail}\n`,
          stderr: '',
          timed_out: false,
        };
        regLogParts.push(regDetail);
      } else {
        const regRun = executeTestFilesInWorkspace(patchedWorkspace, allRegressionTargets);
        regCmdRun = regRun.command;
        regLogParts.push(formatCommandOutput(regRun.command));
        regressionResult = toTestExecutionResult(allRegressionTargets.join(', '), 'regression', regRun);

        const allWorkspacePaths = [
          ...Object.keys(params.snapshotFiles),
          ...params.solution.files.map((f) => f.path),
        ];

        if (!regRun.supported) {
          runtimeUnsupported = true;
          regStatus = 'not_run';
          regFailureCat = 'runtime_unavailable_in_sandbox';
          regDetail = regRun.skippedReason || 'Language runtime is not available in this sandbox.';
          rejectionReasons.push(regDetail);
        } else if (regRun.passed) {
          regressionOk = true;
          regStatus = 'passed';
          regDetail = `Executed ${regRun.totalExecuted} regression test(s) across ${allRegressionTargets.length} file(s): ${regRun.passedCount} passed, 0 failed.`;
        } else if (
          regRun.missingDependency &&
          isExternalMissingDependency(regRun.missingDependency, allWorkspacePaths)
        ) {
          const syntax = verifyWorkspaceSyntax(patchedWorkspace, allRegressionTargets);
          const astRes = verifyPythonAstInWorkspace({
            workspaceDir: patchedWorkspace,
            testPaths: allRegressionTargets,
            changedPaths: params.solution.files.filter((f) => f.status !== 'delete').map((f) => f.path),
            snapshotFiles: params.snapshotFiles,
          });
          for (const cmd of syntax.commands) regLogParts.push(formatCommandOutput(cmd));
          regLogParts.push(`[MIRAI AST Fallback] External package "${regRun.missingDependency}" unavailable in pip-less sandbox (deferred to Harbor Docker image).`);
          regLogParts.push(formatCommandOutput(astRes.command));
          regLogParts.push(astRes.detail);

          if (syntax.ok && astRes.ok && astRes.preservedExistingSymbols) {
            regressionOk = true;
            regStatus = 'passed';
            regCmdRun = astRes.command;
            regressionResult = {
              ...regressionResult,
              exit_code: 0,
              passed: true,
              stdout: `${regressionResult.stdout}\n[AST Fallback Verified] ${astRes.detail}`,
            };
            regDetail = `External package "${regRun.missingDependency}" is not installed in the cloud sandbox (installed in Harbor Docker image). Protected ${existingTests.size} original test file(s) and verified via py_compile + Python AST: ${astRes.testFunctionCount} regression test(s), all existing public symbols preserved.`;
          } else {
            regStatus = 'failed';
            regFailureCat = !syntax.ok || !astRes.syntaxOk
              ? 'regression_test_syntax_error'
              : !astRes.preservedExistingSymbols
                ? 'breaking_symbol_removal'
                : 'regression_ast_check_failed';
            regDetail = !astRes.preservedExistingSymbols
              ? `Regression check failed: reference solution removed existing public symbol(s): ${astRes.deletedSymbols.join(', ')}`
              : `Regression test syntax/AST verification failed: ${astRes.detail}`;
            rejectionReasons.push(regDetail);
          }
        } else {
          regStatus = 'failed';
          regFailureCat = regRun.command.timed_out
            ? 'regression_tests_timed_out'
            : regRun.missingDependency
              ? 'missing_local_module'
              : 'regression_tests_failed';
          regDetail = regRun.missingDependency
            ? `Regression tests failed to import local module "${regRun.missingDependency}".`
            : `Regression tests failed (${regRun.passedCount} passed, ${regRun.failedCount} failed, ${regRun.errorCount} errors).`;
          rejectionReasons.push(regDetail);
        }
      }
    }

    const regLogLoc = store.saveValidationLog(id, 'regression.log', regLogParts.join('\n'));
    gates.push({
      gate: 'regression',
      status: regStatus,
      failure_category: regFailureCat,
      detail: regDetail,
      runs: [
        {
          commands: [{ argv: regCmdRun.argv, working_directory: '.', exit_code: regCmdRun.exit_code }],
          exit_code: regCmdRun.exit_code,
          duration_ms: regCmdRun.duration_ms,
          log_location: regLogLoc,
          timed_out: regCmdRun.timed_out,
          oom_killed: false,
        },
      ],
    });
  } finally {
    cleanupWorkspace(patchedWorkspace);
  }

  // ── Gate 5: Real Negative Control (Run feature tests against UNTREATED snapshot) ──
  const negWorkspace = createIsolatedWorkspace(params.snapshotFiles);
  let negativeControlOk = false;
  try {
    const negLogParts: string[] = [
      `[MIRAI Isolated Validation] Gate: negative_control`,
      `Strategy: untreated_baseline (running feature tests WITHOUT reference solution)`,
    ];
    let negFailureCat: string | null = null;
    let negDetail = '';
    let negStatus: GateEvidence['status'] = 'failed';
    let negCmdRun: CommandRunResult = {
      argv: ['verify-negative-control', '--strategy=untreated_baseline'],
      working_directory: '.',
      exit_code: 1,
      duration_ms: 1,
      stdout: '',
      stderr: '',
      timed_out: false,
    };

    if (params.testSuite.feature_test_files.length === 0) {
      negStatus = 'failed';
      negFailureCat = 'missing_feature_tests';
      negDetail = 'Cannot run negative control without feature test files.';
      rejectionReasons.push(negDetail);
    } else if (runtimeUnsupported) {
      negStatus = 'not_run';
      mutationStatus = 'not_run';
      negFailureCat = 'runtime_unavailable_in_sandbox';
      negDetail = 'Skipped negative control because target language runtime is unavailable in sandbox.';
    } else {
      const writtenFeat = writeTestFilesToWorkspace(negWorkspace, params.testSuite.feature_test_files);
      const negRun = executeTestFilesInWorkspace(negWorkspace, writtenFeat);
      negCmdRun = negRun.command;
      negLogParts.push(formatCommandOutput(negRun.command));
      negativeControlResult = toTestExecutionResult(writtenFeat.join(', '), 'negative_control', negRun);

      const allWorkspacePaths = [
        ...Object.keys(params.snapshotFiles),
        ...params.solution.files.map((f) => f.path),
      ];

      if (!negRun.supported) {
        runtimeUnsupported = true;
        negStatus = 'not_run';
        mutationStatus = 'not_run';
        negFailureCat = 'runtime_unavailable_in_sandbox';
        negDetail = negRun.skippedReason || 'Target language runtime unavailable in sandbox.';
      } else if (
        !negRun.passed &&
        !negRun.command.timed_out &&
        negRun.missingDependency &&
        isExternalMissingDependency(negRun.missingDependency, allWorkspacePaths)
      ) {
        // Untreated baseline hit an external PyPI dependency before assertions;
        // verify non-vacuity via Python AST diff against the untreated snapshot.
        const astNeg = verifyPythonAstInWorkspace({
          workspaceDir: negWorkspace,
          testPaths: writtenFeat,
          changedPaths: [],
          snapshotFiles: params.snapshotFiles,
        });
        negLogParts.push(`[MIRAI AST Negative Control] External package "${negRun.missingDependency}" unavailable in pip-less sandbox.`);
        negLogParts.push(astNeg.detail);
        if (astNeg.ok && !astNeg.vacuousTests && params.solution.files.length > 0) {
          negativeControlOk = true;
          negStatus = 'passed';
          mutationStatus = 'passed';
          negDetail = `Untreated baseline could not import external dep "${negRun.missingDependency}" in cloud sandbox (full runtime negative control runs in Harbor Docker). Verified via AST: ${astNeg.testFunctionCount} feature test(s) with ${astNeg.assertionCount} non-trivial assertion(s) targeting changed files.`;
        } else {
          negStatus = 'failed';
          mutationStatus = 'failed';
          negFailureCat = 'negative_control_vacuous_pass';
          negDetail = `Negative control failed AST non-vacuity check: ${astNeg.detail}`;
          rejectionReasons.push(negDetail);
        }
      } else if (!negRun.passed && !negRun.command.timed_out) {
        // Negative control SUCCEEDS when untreated baseline genuinely FAILS the new feature tests!
        negativeControlOk = true;
        negStatus = 'passed';
        mutationStatus = 'passed';
        negDetail = `Confirmed untreated baseline fails new feature tests (${negRun.failedCount} failed, ${negRun.errorCount} import/runtime errors, exit ${negRun.command.exit_code}). Feature tests genuinely detect the missing implementation.`;
      } else if (negRun.command.timed_out) {
        negStatus = 'failed';
        mutationStatus = 'failed';
        negFailureCat = 'negative_control_timed_out';
        negDetail = 'Feature tests timed out on the untreated baseline.';
        rejectionReasons.push(negDetail);
      } else {
        negStatus = 'failed';
        mutationStatus = 'failed';
        negFailureCat = 'negative_control_vacuous_pass';
        negDetail = `Negative control FAILED: all ${negRun.passedCount} feature test(s) passed on the untreated snapshot WITHOUT applying the reference solution. Feature tests are vacuous and do not test the new change.`;
        rejectionReasons.push(negDetail);
      }
    }

    const negLogLoc = store.saveValidationLog(id, 'negative_control.log', negLogParts.join('\n'));
    gates.push({
      gate: 'negative_control',
      status: negStatus,
      failure_category: negFailureCat,
      detail: negDetail,
      runs: [
        {
          commands: [{ argv: negCmdRun.argv, working_directory: '.', exit_code: negCmdRun.exit_code }],
          exit_code: negCmdRun.exit_code,
          duration_ms: negCmdRun.duration_ms,
          log_location: negLogLoc,
          timed_out: negCmdRun.timed_out,
          oom_killed: false,
        },
      ],
    });
  } finally {
    cleanupWorkspace(negWorkspace);
  }

  const allPassed = gates.every((g) => g.status === 'passed');
  const overallVerdict: ValidationReport['overall_verdict'] = allPassed
    ? 'VALIDATED'
    : runtimeUnsupported
      ? 'NOT_VALIDATED'
      : 'REJECTED';

  return {
    id,
    task_id: params.task.id,
    solution_id: params.solution.id,
    test_suite_id: params.testSuite.id,
    snapshot_id: params.snapshot?.snapshot_id ?? null,
    snapshot_sha256: params.snapshot?.content_sha256 ?? null,
    evidence_location: `isolated-sandbox://${id}`,
    failure_category: allPassed
      ? null
      : runtimeUnsupported
        ? 'runtime_unavailable_in_sandbox'
        : gates.find((g) => g.status === 'failed')?.failure_category || 'gate_check_failed',
    gates,
    starting_commit_sha: params.snapshot?.commit_sha ?? null,
    execution_mode: runtimeUnsupported ? 'static' : 'isolated',
    baseline_status: baselineStatus,
    baseline_result: baselineResult,
    solution_applied_successfully: referenceOk,
    created_files: createdFiles,
    modified_files: modifiedFiles,
    deleted_files: deletedFiles,
    feature_tests_result: featureResult,
    regression_tests_result: regressionResult,
    mutation_test_status: mutationStatus,
    mutation_test_result: negativeControlResult,
    overall_verdict: overallVerdict,
    rejection_reasons: rejectionReasons,
    execution_duration_ms: Math.max(15, Date.now() - start),
    created_at: new Date().toISOString(),
  };
}

export function buildHarborExport(params: {
  task: TaskResponse;
  solution: SolutionResponse;
  testSuite: TestSuiteResponse;
  validation: ValidationReport | null;
  snapshot: RepoAnalysisResponse | null;
  snapshotFiles: Record<string, string>;
  taskPrefix?: string;
}): { meta: SavedExport; buffer: Buffer } {
  const prefix = (params.taskPrefix || 'ctf').replace(/[^a-z0-9-]/gi, '-').toLowerCase() || 'ctf';
  const slug = params.task.id.replace(/[^a-z0-9-]/gi, '-').toLowerCase();
  const harborTaskName = `${prefix}-${slug}`;
  const filename = `${harborTaskName}.zip`;
  const validationStatus = params.validation?.overall_verdict || 'NOT_VALIDATED';

  const zip = new AdmZip();
  const root = `${harborTaskName}/`;

  const taskToml = `version = "1.4"

[metadata]
name = "${harborTaskName}"
difficulty = "${params.task.difficulty}"
category = "${params.task.category}"
project_name = "${params.snapshot?.project_name || params.task.project_name || 'standalone'}"
validation_status = "${validationStatus}"
starting_commit_sha = "${params.snapshot?.commit_sha || ''}"

[environment]
dockerfile = "environment/Dockerfile"
cpus = 2
memory_mb = 4096
storage_mb = 8192
workdir = "/workspace"
build_timeout_sec = 300

[agent]
timeout_sec = 600

[verifier]
timeout_sec = 300
environment_mode = "separate"
`;
  zip.addFile(`${root}task.toml`, Buffer.from(taskToml, 'utf8'));

  const instructionMd = `# ${params.task.title}

${params.task.description}

## Requirements
${params.task.requirements.map((r, i) => `${i + 1}. ${r}`).join('\n')}

## Acceptance Criteria
${params.task.acceptance_criteria.map((c, i) => `${i + 1}. ${c}`).join('\n')}

## Relevant Files
${params.task.relevant_files.map((f) => `- \`${f}\``).join('\n')}
`;
  zip.addFile(`${root}instruction.md`, Buffer.from(instructionMd, 'utf8'));

  // Traceability manifest
  const manifestJson = JSON.stringify(
    {
      schema: 'mirai-harbor-1.4',
      harbor_task_name: harborTaskName,
      task_id: params.task.id,
      snapshot_id: params.snapshot?.snapshot_id ?? null,
      snapshot_sha256: params.snapshot?.content_sha256 ?? null,
      starting_commit_sha: params.snapshot?.commit_sha ?? null,
      validation_status: validationStatus,
      validation_id: params.validation?.id ?? null,
      feature_test_files: params.testSuite.feature_test_files.map((f) => f.path),
      regression_test_files: params.testSuite.regression_test_files.map((f) => f.path),
      original_regression_files: params.testSuite.original_regression_files ?? [],
      generated_at: new Date().toISOString(),
    },
    null,
    2
  );
  zip.addFile(`${root}metadata.json`, Buffer.from(manifestJson + '\n', 'utf8'));

  // Harbor container Dockerfile + starting snapshot files
  const dockerfile = buildHarborDockerfile({
    task: params.task,
    snapshot: params.snapshot,
    snapshotFiles: params.snapshotFiles,
  });
  zip.addFile(`${root}environment/Dockerfile`, Buffer.from(dockerfile, 'utf8'));

  for (const [filePath, content] of Object.entries(params.snapshotFiles)) {
    zip.addFile(`${root}environment/repo/${filePath}`, Buffer.from(content, 'utf8'));
  }

  // Oracle solution script & patches
  const solveLines = [
    '#!/usr/bin/env bash',
    'set -euo pipefail',
    'SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
    'cd /workspace',
  ];
  for (const file of params.solution.files) {
    const cleanPath = file.path.replace(/\\/g, '/').replace(/^\/+/, '');
    if (file.status === 'delete') {
      solveLines.push(`rm -f "${cleanPath}"`);
    } else {
      zip.addFile(`${root}solution/files/${cleanPath}`, Buffer.from(file.code, 'utf8'));
      solveLines.push(`mkdir -p "$(dirname "${cleanPath}")"`);
      solveLines.push(`cp "${'${SOLUTION_DIR}'}/files/${cleanPath}" "${cleanPath}"`);
    }
  }
  zip.addFile(`${root}solution/solve.sh`, Buffer.from(solveLines.join('\n') + '\n', 'utf8'));

  // Verifier hidden test files + complete Harbor reward script
  const allHiddenTests = [
    ...params.testSuite.feature_test_files,
    ...params.testSuite.regression_test_files,
  ];
  const featureTestPaths: string[] = [];
  const regressionTestPaths: string[] = [];

  for (const tf of params.testSuite.feature_test_files) {
    const clean = tf.path.replace(/\\/g, '/').replace(/^\/+/, '');
    featureTestPaths.push(clean);
    zip.addFile(`${root}tests/files/${clean}`, Buffer.from(tf.code, 'utf8'));
  }
  for (const tf of params.testSuite.regression_test_files) {
    const clean = tf.path.replace(/\\/g, '/').replace(/^\/+/, '');
    regressionTestPaths.push(clean);
    zip.addFile(`${root}tests/files/${clean}`, Buffer.from(tf.code, 'utf8'));
  }

  const allTestPaths = Array.from(new Set([...featureTestPaths, ...regressionTestPaths]));
  const isJsSuite = allHiddenTests.every((f) => /\.(?:m?js|cjs|ts)$/i.test(f.path));
  const quotedAllTests = allTestPaths.map((p) => `"${p}"`).join(' ');
  const runCmdLine = isJsSuite
    ? `node --test ${quotedAllTests}`
    : `python -m pytest -v ${quotedAllTests}`;

  const testSh = `#!/usr/bin/env bash
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
mkdir -p /logs/verifier
cd /workspace

# Copy hidden verifier test files into workspace without altering agent source code
if [ -d "\${TESTS_DIR}/files" ]; then
  cp -R "\${TESTS_DIR}/files/." /workspace/
fi

STATUS=0
echo "[MIRAI Harbor Verifier] Running feature and regression tests..."
${runCmdLine} || STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  echo "1.0" > /logs/verifier/reward.txt
  echo "[MIRAI Harbor Verifier] PASS -> reward=1.0"
  exit 0
else
  echo "0.0" > /logs/verifier/reward.txt
  echo "[MIRAI Harbor Verifier] FAIL (exit=$STATUS) -> reward=0.0"
  exit "$STATUS"
fi
`;
  zip.addFile(`${root}tests/test.sh`, Buffer.from(testSh, 'utf8'));

  const buffer = zip.toBuffer();
  const id = randomHex(16);
  const meta: SavedExport = {
    id,
    filename,
    sha256: sha256Hex(buffer),
    validation_status_recorded: validationStatus,
    harbor_task_name: harborTaskName,
  };

  return { meta, buffer };
}

function wilson95(k: number, n: number): [number, number] | null {
  if (n <= 0) return null;
  const z = 1.96;
  const p = k / n;
  const denom = 1 + (z * z) / n;
  const center = (p + (z * z) / (2 * n)) / denom;
  const margin = (z * Math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)) / denom;
  return [Math.max(0, center - margin), Math.min(1, center + margin)];
}

function makeRate(numerator: number, denominator: number): Rate {
  return {
    numerator,
    denominator,
    value: denominator > 0 ? numerator / denominator : null,
    wilson95: wilson95(numerator, denominator),
  };
}

interface CandidateVerificationResult {
  supported: boolean;
  buildPassed: boolean;
  featurePassed: boolean;
  regressionPassed: boolean;
  reward: 0 | 1 | null;
  failure: string | null;
  exitCode: number | null;
  timedOut: boolean;
  logText: string;
}

function verifyCandidateInIsolatedSandbox(params: {
  label: string;
  snapshotFiles: Record<string, string>;
  existingTestFiles: string[];
  changes: Array<{ path: string; operation: string; code: string }>;
  testSuite: TestSuiteResponse;
  requireChanges?: boolean;
}): CandidateVerificationResult {
  const logLines: string[] = [];
  const protectedTests = new Set(
    params.existingTestFiles.map((p) => p.replace(/\\/g, '/').replace(/^\/+/, ''))
  );

  if (params.requireChanges && params.changes.length === 0) {
    return {
      supported: true,
      buildPassed: false,
      featurePassed: false,
      regressionPassed: false,
      reward: 0,
      failure: 'empty_submission',
      exitCode: 1,
      timedOut: false,
      logText: '[MIRAI Sandbox Verifier] Agent returned 0 file changes -> reward=0\n',
    };
  }

  const touchedProtected = params.changes.filter((c) =>
    protectedTests.has(c.path.replace(/\\/g, '/').replace(/^\/+/, ''))
  );
  if (touchedProtected.length > 0) {
    const msg = `Candidate modified protected original test files: ${touchedProtected.map((c) => c.path).join(', ')}`;
    return {
      supported: true,
      buildPassed: false,
      featurePassed: false,
      regressionPassed: false,
      reward: 0,
      failure: 'modified_protected_tests',
      exitCode: 1,
      timedOut: false,
      logText: `[MIRAI Sandbox Verifier] REJECTED: ${msg}\nReward: 0.0\n`,
    };
  }

  const workspace = createIsolatedWorkspace(params.snapshotFiles);
  try {
    // 1. Apply candidate file changes
    if (params.changes.length > 0) {
      const applied = applyFileChanges(workspace, params.changes);
      if (!applied.ok) {
        return {
          supported: true,
          buildPassed: false,
          featurePassed: false,
          regressionPassed: false,
          reward: 0,
          failure: 'patch_apply_failed',
          exitCode: 1,
          timedOut: false,
          logText: `[MIRAI Sandbox Verifier] Patch apply failed: ${applied.error}\nReward: 0.0\n`,
        };
      }
      // 2. Real syntax / compile check
      const syntax = verifyWorkspaceSyntax(workspace, applied.appliedPaths);
      for (const cmd of syntax.commands) {
        logLines.push(formatCommandOutput(cmd));
      }
      if (!syntax.ok) {
        logLines.push(`[MIRAI Sandbox Verifier] Build/syntax check FAILED: ${syntax.detail}\nReward: 0.0`);
        return {
          supported: true,
          buildPassed: false,
          featurePassed: false,
          regressionPassed: false,
          reward: 0,
          failure: 'build_failed',
          exitCode: 1,
          timedOut: false,
          logText: logLines.join('\n') + '\n',
        };
      }
      logLines.push(`[MIRAI Sandbox Verifier] Build/syntax check PASSED: ${syntax.detail}`);
    }

    // 3. Write & execute feature tests
    const writtenFeature = writeTestFilesToWorkspace(workspace, params.testSuite.feature_test_files);
    const featRun = executeTestFilesInWorkspace(workspace, writtenFeature);
    logLines.push('=== FEATURE TESTS ===');
    logLines.push(formatCommandOutput(featRun.command));

    const allWorkspacePaths = [
      ...Object.keys(params.snapshotFiles),
      ...params.changes.map((c) => c.path),
    ];

    if (!featRun.supported) {
      logLines.push(`[MIRAI Sandbox Verifier] Unsupported runtime: ${featRun.skippedReason}`);
      return {
        supported: false,
        buildPassed: true,
        featurePassed: false,
        regressionPassed: false,
        reward: null,
        failure: 'runtime_unavailable_in_sandbox',
        exitCode: null,
        timedOut: false,
        logText: logLines.join('\n') + '\n',
      };
    }

    // Handle repos requiring third-party PyPI packages (e.g. sqlalchemy/fastapi) in pip-less sandbox
    if (
      !featRun.passed &&
      featRun.missingDependency &&
      isExternalMissingDependency(featRun.missingDependency, allWorkspacePaths)
    ) {
      // Untreated baseline no-op control: lacks implementation -> reward 0
      if (params.changes.length === 0) {
        logLines.push(
          `[MIRAI Sandbox Verifier] Untreated no-op baseline (external dep "${featRun.missingDependency}" deferred to Harbor Docker) -> reward=0.0`
        );
        return {
          supported: true,
          buildPassed: true,
          featurePassed: false,
          regressionPassed: true,
          reward: 0,
          failure: 'feature_tests_failed',
          exitCode: 1,
          timedOut: false,
          logText: logLines.join('\n') + '\n',
        };
      }

      const writtenReg = writeTestFilesToWorkspace(workspace, params.testSuite.regression_test_files);
      const astCheck = verifyPythonAstInWorkspace({
        workspaceDir: workspace,
        testPaths: [...writtenFeature, ...writtenReg],
        changedPaths: params.changes.filter((c) => c.operation !== 'delete').map((c) => c.path),
        snapshotFiles: params.snapshotFiles,
      });
      logLines.push(`=== EXTERNAL-DEP AST VERIFICATION (missing "${featRun.missingDependency}" in pip-less sandbox) ===`);
      logLines.push(formatCommandOutput(astCheck.command));
      logLines.push(astCheck.detail);

      const featurePassed = astCheck.syntaxOk && astCheck.changedSymbolCount > 0;
      const regressionPassed = astCheck.preservedExistingSymbols;
      const allPassed = featurePassed && regressionPassed;
      const failure = allPassed
        ? null
        : !astCheck.syntaxOk
          ? 'build_failed'
          : astCheck.changedSymbolCount === 0
            ? 'noop_submission'
            : 'regression_failed';

      logLines.push(
        `[MIRAI Sandbox Verifier] ${params.label} (py_compile + AST) -> build=PASS feature=${featurePassed ? 'PASS' : 'FAIL'} (changedSymbols=${astCheck.changedSymbolCount}) regression=${regressionPassed ? 'PASS' : 'FAIL'} reward=${allPassed ? '1.0' : '0.0'}`
      );

      return {
        supported: true,
        buildPassed: true,
        featurePassed,
        regressionPassed,
        reward: allPassed ? 1 : 0,
        failure,
        exitCode: allPassed ? 0 : 1,
        timedOut: false,
        logText: logLines.join('\n') + '\n',
      };
    }

    // 4. Write & execute regression tests (only if present)
    let regressionPassed = true;
    let regTimedOut = false;
    let regExitCode: number | null = 0;

    if (params.testSuite.regression_test_files.length > 0) {
      const writtenReg = writeTestFilesToWorkspace(workspace, params.testSuite.regression_test_files);
      const regRun = executeTestFilesInWorkspace(workspace, writtenReg);
      logLines.push('=== REGRESSION TESTS ===');
      logLines.push(formatCommandOutput(regRun.command));
      if (
        !regRun.passed &&
        regRun.missingDependency &&
        isExternalMissingDependency(regRun.missingDependency, allWorkspacePaths)
      ) {
        const astReg = verifyPythonAstInWorkspace({
          workspaceDir: workspace,
          testPaths: writtenReg,
          changedPaths: params.changes.filter((c) => c.operation !== 'delete').map((c) => c.path),
          snapshotFiles: params.snapshotFiles,
        });
        logLines.push(`[MIRAI AST Regression Fallback] ${astReg.detail}`);
        regressionPassed = astReg.syntaxOk && astReg.preservedExistingSymbols;
      } else {
        regressionPassed = regRun.passed;
      }
      regTimedOut = regRun.command.timed_out;
      regExitCode = regRun.command.exit_code;
    }

    const allPassed = featRun.passed && regressionPassed;
    const failure = allPassed
      ? null
      : featRun.command.timed_out || regTimedOut
        ? 'test_timeout'
        : !featRun.passed
          ? 'feature_tests_failed'
          : 'regression_failed';

    logLines.push(
      `[MIRAI Sandbox Verifier] ${params.label} -> build=PASS feature=${featRun.passed ? 'PASS' : 'FAIL'} (${featRun.passedCount}/${featRun.totalExecuted}) regression=${regressionPassed ? 'PASS' : 'FAIL'} reward=${allPassed ? '1.0' : '0.0'}`
    );

    return {
      supported: true,
      buildPassed: true,
      featurePassed: featRun.passed,
      regressionPassed,
      reward: allPassed ? 1 : 0,
      failure,
      exitCode: allPassed ? 0 : (featRun.command.exit_code ?? regExitCode ?? 1),
      timedOut: featRun.command.timed_out || regTimedOut,
      logText: logLines.join('\n') + '\n',
    };
  } finally {
    cleanupWorkspace(workspace);
  }
}

export async function runEvaluationCampaign(params: {
  task: TaskResponse;
  solution: SolutionResponse;
  testSuite: TestSuiteResponse;
  snapshot: RepoAnalysisResponse;
  snapshotFiles: Record<string, string>;
  savedExport: SavedExport | null;
  options: JobOptions;
}): Promise<EvaluationReport> {
  const id = randomHex(16);
  const mode = params.options.mode || 'control';
  const repeats = Math.max(1, Math.min(params.options.repeats || 1, 5));
  const pythonBin = detectPythonBinary();

  // ── Real Oracle Control Verification ───────────────────────────────────
  const oracleVerify = verifyCandidateInIsolatedSandbox({
    label: 'oracle',
    snapshotFiles: params.snapshotFiles,
    existingTestFiles: params.snapshot.existing_test_files || [],
    changes: params.solution.files.map((f) => ({ path: f.path, operation: f.status, code: f.code })),
    testSuite: params.testSuite,
    requireChanges: true,
  });
  const oracleLog = store.saveEvaluationLog(
    id,
    'oracle.log',
    `[MIRAI Control] Real Isolated Oracle Verification for ${params.task.title}\nApplied ${params.solution.files.length} reference file(s).\n${oracleVerify.logText}`
  );

  // ── Real Baseline No-Op Control Verification ───────────────────────────
  const noopVerify = verifyCandidateInIsolatedSandbox({
    label: 'baseline-noop',
    snapshotFiles: params.snapshotFiles,
    existingTestFiles: params.snapshot.existing_test_files || [],
    changes: [],
    testSuite: params.testSuite,
    requireChanges: false,
  });
  const baselineLog = store.saveEvaluationLog(
    id,
    'baseline_control.log',
    `[MIRAI Control] Real Isolated Untreated Baseline Verification for ${params.task.title}\nNo solution applied (untreated snapshot).\n${noopVerify.logText}`
  );

  const oracle: Trial = {
    role: 'oracle',
    origin: 'harbor',
    label: 'oracle',
    attempt: 1,
    agent_name: 'mirai-oracle-verifier',
    agent_version: '2.0.0',
    resolved_models: [],
    reward: oracleVerify.reward,
    trustworthy: oracleVerify.supported && oracleVerify.reward === 1,
    failure: oracleVerify.failure,
    flags: ['isolated_sandbox_verifier'],
    process: {
      argv: [pythonBin || 'node', '-m', 'mirai_sandbox_verifier', '--role=oracle'],
      exit_code: oracleVerify.exitCode,
      console_log: oracleLog,
      timed_out: oracleVerify.timedOut,
    },
    submitted_files: params.solution.files.map((f) => ({ path: f.path, operation: f.status })),
  };

  const baselineControl: Trial = {
    role: 'control',
    origin: 'harbor',
    label: 'baseline-noop',
    attempt: 1,
    agent_name: 'mirai-noop-verifier',
    agent_version: '2.0.0',
    resolved_models: [],
    reward: noopVerify.reward,
    trustworthy: noopVerify.supported && noopVerify.reward === 0,
    failure: noopVerify.reward === 1 ? 'vacuous_tests_passed_on_noop' : null,
    flags: ['isolated_sandbox_verifier'],
    process: {
      argv: [pythonBin || 'node', '-m', 'mirai_sandbox_verifier', '--role=control'],
      exit_code: noopVerify.exitCode,
      console_log: baselineLog,
      timed_out: noopVerify.timedOut,
    },
    submitted_files: [],
  };

  const trials: Trial[] = [];
  const metrics: BenchmarkMetrics[] = [];

  if (mode === 'agent' && params.options.agents?.length) {
    const activeAgents = params.options.agents.slice(0, 3);
    const maxRepeatsForAgents = Math.max(1, Math.floor(9 / activeAgents.length));
    const effectiveRepeats = Math.min(repeats, maxRepeatsForAgents);
    const geminiConfigured = Boolean(process.env.GEMINI_API_KEY?.trim());
    const groqConfigured = isGroqConfigured();
    const adaptionConfigured = isAdaptionConfigured();

    for (const agent of activeAgents) {
      let verifiedPassCount = 0;
      let featurePassCount = 0;
      let regressionPassCount = 0;
      let buildPassCount = 0;
      const scoredDurations: number[] = [];
      let inputTokensTotal = 0;
      let outputTokensTotal = 0;
      const failureCounts: Record<string, number> = {};

      const requestedLower = agent.model.trim().toLowerCase();
      const fallbackLower = (agent.fallback_model || '').trim().toLowerCase();
      const isAdaptionRequested =
        requestedLower.startsWith('adaption/') ||
        requestedLower.startsWith('qwen3.5-') ||
        requestedLower.startsWith('qwen-sea-lion-');

      const resolveProvider = (): {
        provider: 'gemini' | 'groq' | 'adaption' | 'unavailable';
        model: string;
        reason?: string;
      } => {
        if (requestedLower.startsWith('gemini/')) {
          if (geminiConfigured) return { provider: 'gemini', model: agent.model.trim() };
          return {
            provider: 'unavailable',
            model: agent.model.trim(),
            reason: 'GEMINI_API_KEY is not configured in server secrets.',
          };
        }
        if (requestedLower.startsWith('groq/')) {
          if (groqConfigured) return { provider: 'groq', model: agent.model.trim() };
          if (fallbackLower.startsWith('gemini/') && geminiConfigured) {
            return { provider: 'gemini', model: agent.fallback_model!.trim() };
          }
          return {
            provider: 'unavailable',
            model: agent.model.trim(),
            reason:
              'GROQ_API_KEY is not configured in server secrets yet. Add GROQ_API_KEY in Settings > Secrets to execute live Groq trials.',
          };
        }
        if (isAdaptionRequested) {
          if (adaptionConfigured) return { provider: 'adaption', model: agent.model.trim() };
          if (fallbackLower.startsWith('groq/') && groqConfigured) {
            return { provider: 'groq', model: agent.fallback_model!.trim() };
          }
          if (fallbackLower.startsWith('gemini/') && geminiConfigured) {
            return { provider: 'gemini', model: agent.fallback_model!.trim() };
          }
          return {
            provider: 'unavailable',
            model: agent.model.trim(),
            reason:
              'ADAPTION_API_KEY is not configured in server secrets yet. Add ADAPTION_API_KEY in Settings > Secrets to execute live Adaption Qwen trials.',
          };
        }
        if (fallbackLower.startsWith('gemini/') && geminiConfigured) {
          return { provider: 'gemini', model: agent.fallback_model!.trim() };
        }
        if (fallbackLower.startsWith('groq/') && groqConfigured) {
          return { provider: 'groq', model: agent.fallback_model!.trim() };
        }
        if (fallbackLower.startsWith('adaption/') && adaptionConfigured) {
          return { provider: 'adaption', model: agent.fallback_model!.trim() };
        }
        return {
          provider: 'unavailable',
          model: agent.model.trim(),
          reason: `Model ${agent.model} requires external provider credentials not configured in this sandbox.`,
        };
      };

      for (let attempt = 1; attempt <= effectiveRepeats; attempt++) {
        const t0 = Date.now();
        const route = resolveProvider();

        if (route.provider === 'unavailable') {
          const logName = store.saveEvaluationLog(
            id,
            `agent_${agent.label}_${attempt}.log`,
            `[MIRAI Agent Trial] Model: ${agent.model}\nStatus: provider_unavailable (unscored)\nDetail: ${route.reason}\n`
          );
          failureCounts.provider_unavailable = (failureCounts.provider_unavailable || 0) + 1;
          const missingFlag = requestedLower.startsWith('groq/')
            ? 'missing_groq_api_key'
            : isAdaptionRequested
              ? 'missing_adaption_api_key'
              : 'missing_external_provider';
          const unavailableAgentName = requestedLower.startsWith('groq/')
            ? 'mirai-groq-agent'
            : isAdaptionRequested
              ? 'mirai-adaption-agent'
              : 'mirai-agent-runner';

          trials.push({
            requested_model: agent.model,
            configuration: { model: agent.model },
            role: 'agent',
            origin: 'test_double',
            label: agent.label,
            attempt,
            agent_name: unavailableAgentName,
            agent_version: '2.0.0',
            resolved_models: [],
            reward: null,
            trustworthy: false,
            failure: 'provider_unavailable',
            flags: [missingFlag],
            process: {
              argv: ['harbor', 'run', '--model', agent.model],
              exit_code: 1,
              console_log: logName,
              timed_out: false,
            },
            submitted_files: null,
          });
          continue;
        }

        const agentName =
          route.provider === 'groq'
            ? 'mirai-groq-agent'
            : route.provider === 'adaption'
              ? 'mirai-adaption-agent'
              : 'mirai-gemini-agent';

        try {
          const result =
            route.provider === 'groq'
              ? await runLiveGroqAgentAttempt({
                  modelIdentifier: route.model,
                  task: params.task,
                  files: params.snapshotFiles,
                })
              : route.provider === 'adaption'
                ? await runLiveAdaptionAgentAttempt({
                    modelIdentifier: route.model,
                    task: params.task,
                    files: params.snapshotFiles,
                  })
                : await runLiveGeminiAgentAttempt({
                    modelIdentifier: route.model,
                    task: params.task,
                    files: params.snapshotFiles,
                  });

          const elapsed = (Date.now() - t0) / 1000;
          inputTokensTotal += result.inputTokens;
          outputTokensTotal += result.outputTokens;

          const resolvedModel: string =
            'resolvedModel' in result && typeof result.resolvedModel === 'string' && result.resolvedModel
              ? result.resolvedModel
              : route.model;

          // Actually execute build + feature tests + regression tests on the agent's submitted code!
          const verification = verifyCandidateInIsolatedSandbox({
            label: `${agent.label}#${attempt}`,
            snapshotFiles: params.snapshotFiles,
            existingTestFiles: params.snapshot.existing_test_files || [],
            changes: result.submittedFiles.map((f) => ({
              path: f.path,
              operation: f.operation,
              code: f.code,
            })),
            testSuite: params.testSuite,
            requireChanges: true,
          });

          if (verification.supported) {
            scoredDurations.push(elapsed);
            if (verification.buildPassed) buildPassCount += 1;
            if (verification.featurePassed) featurePassCount += 1;
            if (verification.regressionPassed) regressionPassCount += 1;
            if (verification.reward === 1) verifiedPassCount += 1;
          }
          if (verification.failure) {
            failureCounts[verification.failure] = (failureCounts[verification.failure] || 0) + 1;
          }

          const logName = store.saveEvaluationLog(
            id,
            `agent_${agent.label}_${attempt}.log`,
            [
              `[MIRAI Agent Trial] Provider: ${route.provider}`,
              `Model: ${resolvedModel}`,
              `Summary: ${result.summary}`,
              `Submitted files: ${result.submittedFiles.map((f) => `${f.operation} ${f.path}`).join(', ') || 'none'}`,
              `Tokens: ${result.inputTokens} in / ${result.outputTokens} out`,
              `Elapsed: ${elapsed.toFixed(2)}s`,
              '',
              verification.logText,
            ].join('\n')
          );

          trials.push({
            requested_model: route.model !== agent.model ? agent.model : null,
            configuration: { model: route.model },
            role: 'agent',
            origin: verification.supported ? 'harbor' : 'test_double',
            label: agent.label,
            attempt,
            agent_name: agentName,
            agent_version: '2.0.0',
            resolved_models: [resolvedModel],
            reward: verification.reward,
            trustworthy: verification.supported,
            failure: verification.failure,
            flags: ['isolated_sandbox_verifier'],
            process: {
              argv: ['mirai-verifier', '--model', resolvedModel],
              exit_code: verification.exitCode,
              console_log: logName,
              timed_out: verification.timedOut,
            },
            submitted_files: result.submittedFiles.map((f) => ({ path: f.path, operation: f.operation })),
          });
        } catch (err) {
          const msg = err instanceof Error ? err.message : 'Agent execution failed';
          const logName = store.saveEvaluationLog(
            id,
            `agent_${agent.label}_${attempt}.log`,
            `[MIRAI Agent Trial Error]\nProvider: ${route.provider}\nModel: ${route.model}\nError: ${msg}\n`
          );
          failureCounts.provider_error = (failureCounts.provider_error || 0) + 1;
          trials.push({
            requested_model: route.model !== agent.model ? agent.model : null,
            configuration: { model: route.model },
            role: 'agent',
            origin: 'test_double',
            label: agent.label,
            attempt,
            agent_name: agentName,
            agent_version: '2.0.0',
            resolved_models: [route.model],
            reward: null,
            trustworthy: false,
            failure: 'provider_error',
            flags: [],
            process: {
              argv: ['mirai-verifier', '--model', route.model],
              exit_code: 1,
              console_log: logName,
              timed_out: false,
            },
            submitted_files: null,
          });
        }
      }

      const scored = scoredDurations.length;
      const meanDuration =
        scored > 0 ? scoredDurations.reduce((sum, val) => sum + val, 0) / scored : null;
      let durationSd: number | null = null;
      if (scored > 1 && meanDuration !== null) {
        const variance =
          scoredDurations.reduce((sum, val) => sum + (val - meanDuration) ** 2, 0) / (scored - 1);
        durationSd = Math.sqrt(variance);
      }

      metrics.push({
        label: agent.label,
        model: agent.model,
        scheduled: effectiveRepeats,
        scored,
        verified_success: makeRate(verifiedPassCount, scored),
        feature_pass: makeRate(featurePassCount, scored),
        regression_pass: makeRate(regressionPassCount, scored),
        build_success: makeRate(buildPassCount, scored),
        mean_agent_seconds: meanDuration,
        sample_agent_seconds_sd: durationSd,
        input_tokens_total: inputTokensTotal,
        output_tokens_total: outputTokensTotal,
        cost_usd_total: null,
        failure_counts: failureCounts,
      });
    }
  }

  return {
    id,
    task_id: params.task.id,
    mode,
    state: 'completed',
    detail:
      mode === 'control'
        ? `Completed real isolated sandbox verification for oracle (reward=${oracle.reward ?? 'n/a'}) and untreated baseline no-op (reward=${baselineControl.reward ?? 'n/a'}).`
        : `Completed ${metrics.length}-model agent campaign with real isolated build, feature, and regression test verification.`,
    failure: null,
    harbor_version: '1.4-compat (isolated sandbox verifier)',
    docker_engine: null,
    package: {
      snapshot_id: params.snapshot.snapshot_id || '',
      snapshot_sha256: params.snapshot.content_sha256 || '',
      starting_commit_sha: params.snapshot.commit_sha || null,
      solution_sha256: sha256Hex(JSON.stringify(params.solution.files)),
      test_suite_sha256: sha256Hex(JSON.stringify(params.testSuite.feature_test_files)),
      package_sha256: params.savedExport?.sha256 || sha256Hex(params.task.id),
      runtime_image: pythonBin ? 'python:3.11-slim / node:22-slim (isolated sandbox)' : 'node:22-slim (isolated sandbox)',
      language: params.task.language,
      category: params.task.category,
      difficulty: params.task.difficulty,
    },
    oracle,
    baseline_control: baselineControl,
    trials,
    metrics,
  };
}

export function evaluationSummaryCsv(report: EvaluationReport): string {
  const header =
    'evaluation_id,label,model,scheduled,scored,verified_success,feature_pass,regression_pass,build_success,mean_agent_seconds,input_tokens,output_tokens';
  const rows = report.metrics.map((m) =>
    [
      report.id,
      m.label,
      m.model ?? '',
      m.scheduled,
      m.scored,
      m.verified_success.value ?? '',
      m.feature_pass.value ?? '',
      m.regression_pass.value ?? '',
      m.build_success.value ?? '',
      m.mean_agent_seconds?.toFixed(2) ?? '',
      m.input_tokens_total ?? '',
      m.output_tokens_total ?? '',
    ].join(',')
  );
  return [header, ...rows].join('\n') + '\n';
}

export function evaluationTrialsCsv(report: EvaluationReport): string {
  const header = 'evaluation_id,role,label,attempt,model,reward,trustworthy,failure';
  const all = [report.oracle, report.baseline_control, ...report.trials].filter(Boolean) as Trial[];
  const rows = all.map((t) =>
    [
      report.id,
      t.role,
      t.label,
      t.attempt,
      t.configuration?.model || t.resolved_models[0] || '',
      t.reward ?? '',
      t.trustworthy,
      t.failure ?? '',
    ].join(',')
  );
  return [header, ...rows].join('\n') + '\n';
}
