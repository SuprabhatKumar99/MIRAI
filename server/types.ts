export type Kind = 'solution' | 'tests' | 'validation' | 'export' | 'evaluation';

export interface RepoAnalysisResponse {
  project_name: string;
  source_type: 'github' | 'zip';
  detected_languages: string[];
  detected_frameworks: string[];
  file_count: number;
  folder_structure: string[];
  readme_summary: string | null;
  analysis_status: 'pending' | 'completed' | 'failed';
  commit_sha: string | null;
  snapshot_id: string | null;
  content_sha256: string | null;
  repository_identity: string | null;
  retrieved_at: string | null;
  test_frameworks: string[];
  existing_test_files: string[];
  baseline_test_commands: Array<{ framework: string; working_directory: string; argv: string[] }>;
}

export interface TaskResponse {
  id: string;
  topic: string;
  difficulty: string;
  language: string;
  category: string;
  title: string;
  description: string;
  requirements: string[];
  acceptance_criteria: string[];
  generated_at: string;
  project_name?: string | null;
  relevant_files: string[];
  snapshot_id?: string | null;
  starting_commit_sha?: string | null;
}

export interface SolutionFile {
  path: string;
  status: 'create' | 'modify' | 'delete';
  language: string;
  code: string;
  explanation: string;
  requirement_ids?: string[];
  acceptance_criteria_ids?: string[];
  base_sha256?: string | null;
  result_sha256?: string | null;
}

export interface SolutionResponse {
  id: string;
  task_id: string;
  task_title: string;
  snapshot_id?: string | null;
  context_files?: Array<{ path: string; kind: 'source' | 'test' | 'config' | 'readme'; complete: boolean }>;
  requirements?: Array<{ id: string; text: string }>;
  acceptance_criteria?: Array<{ id: string; text: string }>;
  structural_check?: {
    status: 'applied_to_snapshot' | 'not_checked';
    execution_status: 'not_run';
    model_attempts: number;
    input_bytes: number;
    estimated_input_tokens: number;
  } | null;
  architecture_notes: string;
  files: SolutionFile[];
  testing_guide: string;
  verification_steps: string[];
  generated_at: string;
}

export interface TestCase {
  id?: string;
  name: string;
  test_kind: 'feature' | 'regression';
  case_type: string;
  description: string;
  target_criterion?: string | null;
  expected_behavior: string;
  requirement_ids?: string[];
  acceptance_criteria_ids?: string[];
  baseline_evidence?: Array<{ path: string; excerpt: string }>;
  negative_control?: {
    strategy: 'untreated_baseline' | 'mutated_reference';
    behavior_to_break: string;
    expected_failure: 'assertion_failure';
  } | null;
}

export interface TestFile {
  path: string;
  test_kind: 'feature' | 'regression';
  framework?: string;
  code: string;
  test_cases: TestCase[];
  dependencies?: Array<{ distribution: string; declared_in: string }>;
  content_sha256?: string | null;
}

export interface CoverageRow {
  id: string;
  text: string;
  feature_tests: Array<{ file_path: string; selector: string; case_id: string }>;
  suggested_scenarios: string[];
}

export interface TestSuiteResponse {
  id: string;
  task_id: string;
  task_title: string;
  repository: {
    project_name: string;
    source_type: 'github' | 'zip' | 'local' | 'unknown';
    starting_commit_sha?: string | null;
    detected_language: string;
    detected_frameworks: string[];
    test_framework: string;
    test_framework_config_file?: string | null;
    existing_test_files: string[];
    environment_info?: Record<string, string>;
  };
  feature_test_files: TestFile[];
  regression_test_files: TestFile[];
  original_regression_files?: Array<{ path: string; sha256: string }>;
  coverage_matrix?: CoverageRow[];
  requirement_coverage?: CoverageRow[];
  execution_plan?: {
    working_directory: string;
    feature: Array<{ framework: string; working_directory: string; argv: string[] }>;
    original_regression: Array<{ framework: string; working_directory: string; argv: string[] }>;
    generated_regression: Array<{ framework: string; working_directory: string; argv: string[] }>;
  } | null;
  quality_status?: 'legacy_unverified' | 'structurally_checked';
  execution_status?: 'not_run';
  total_feature_tests: number;
  total_regression_tests: number;
  coverage_summary: string;
  execution_instructions: string;
  generated_at: string;
}

export interface GateEvidence {
  gate: 'baseline' | 'reference' | 'feature' | 'regression' | 'negative_control';
  status: 'passed' | 'failed' | 'not_run';
  failure_category?: string | null;
  detail?: string | null;
  runs: Array<{
    commands?: Array<{ argv: string[]; working_directory: string; exit_code: number | null }>;
    exit_code: number | null;
    duration_ms: number;
    log_location?: string | null;
    timed_out: boolean;
    oom_killed: boolean;
  }>;
}

export interface TestExecutionResult {
  test_file: string;
  test_kind: string;
  exit_code: number;
  duration_ms: number;
  stdout: string;
  stderr: string;
  passed: boolean;
  individual_cases?: Array<Record<string, unknown>>;
}

export interface ValidationReport {
  id: string;
  task_id: string;
  solution_id: string;
  test_suite_id: string;
  snapshot_id?: string | null;
  snapshot_sha256?: string | null;
  evidence_location?: string | null;
  failure_category?: string | null;
  gates?: GateEvidence[];
  starting_commit_sha?: string | null;
  execution_mode: 'static' | 'trusted_local' | 'isolated' | 'docker' | 'harbor';
  baseline_status: 'passed' | 'failed' | 'not_run' | 'skipped';
  baseline_result?: TestExecutionResult | null;
  solution_applied_successfully: boolean;
  created_files: string[];
  modified_files: string[];
  deleted_files?: string[];
  feature_tests_result?: TestExecutionResult | null;
  regression_tests_result?: TestExecutionResult | null;
  mutation_test_status: 'passed' | 'failed' | 'not_run';
  mutation_test_result?: TestExecutionResult | null;
  overall_verdict: 'VALIDATED' | 'REJECTED' | 'NOT_VALIDATED';
  rejection_reasons: string[];
  execution_duration_ms: number;
  created_at: string;
}

export interface SavedExport {
  id: string;
  filename: string;
  sha256: string;
  validation_status_recorded: string;
  harbor_task_name: string;
}

export interface Rate {
  numerator: number;
  denominator: number;
  value: number | null;
  wilson95: [number, number] | null;
}

export interface BenchmarkMetrics {
  label: string;
  model?: string;
  scheduled: number;
  scored: number;
  verified_success: Rate;
  feature_pass: Rate;
  regression_pass: Rate;
  build_success: Rate;
  mean_agent_seconds: number | null;
  sample_agent_seconds_sd: number | null;
  input_tokens_total: number | null;
  output_tokens_total: number | null;
  cost_usd_total: number | null;
  failure_counts: Record<string, number>;
}

export interface Trial {
  requested_model?: string | null;
  configuration?: { model: string } | null;
  role: 'oracle' | 'agent' | 'control';
  origin: 'harbor' | 'test_double';
  label: string;
  attempt: number;
  agent_name: string | null;
  agent_version: string | null;
  resolved_models: string[];
  reward: 0 | 1 | null;
  trustworthy: boolean;
  failure: string | null;
  flags: string[];
  process: { argv: string[]; exit_code: number | null; console_log: string; timed_out: boolean } | null;
  submitted_files: Array<{ path: string; operation: string }> | null;
}

export interface EvaluationReport {
  id: string;
  task_id: string;
  mode: 'agent' | 'control';
  state: 'queued' | 'running' | 'completed' | 'blocked' | 'quarantined' | 'failed';
  detail: string | null;
  failure: string | null;
  harbor_version: string | null;
  docker_engine: string | null;
  package: {
    snapshot_id: string;
    snapshot_sha256: string;
    starting_commit_sha: string | null;
    solution_sha256: string;
    test_suite_sha256: string;
    package_sha256: string;
    runtime_image: string;
    language: string;
    category: string;
    difficulty: string;
  } | null;
  oracle: Trial | null;
  baseline_control: Trial | null;
  trials: Trial[];
  metrics: BenchmarkMetrics[];
}

export interface Job {
  id: string;
  task_id: string;
  owner_id?: string | null;
  kind: Kind;
  revision: number;
  state: 'queued' | 'running' | 'completed' | 'cancelled' | 'interrupted' | 'failed' | 'superseded';
  result_id: string | null;
  cancel_requested: boolean;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobOptions {
  task_prefix?: string;
  mode?: 'agent' | 'control';
  agents?: Array<{ label: string; model: string; max_turns: number; fallback_model?: string }>;
  repeats?: number;
}

export interface HistoryItem {
  id: string;
  created_at: string;
  status: string | null;
  solution_id: string | null;
  suite_id: string | null;
}

export interface SnapshotRecord {
  analysis: RepoAnalysisResponse;
  files: Record<string, string>;
  projectId: string;
  owner_id?: string | null;
}
