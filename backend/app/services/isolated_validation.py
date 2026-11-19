"""Orchestrate the five required gates over clean, verified snapshot copies."""

import ast
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from app.schemas.repo import RepoAnalysisResponse
from app.schemas.test_suite import TestSuiteResponse
from app.schemas.validation import (
    FailureCategory, GateEvidence, GateName, SandboxRun, TestExecutionResult, ValidationReport, ValidationRequest,
)
from app.services.docker_sandbox import (
    DockerExecutionBackend, SandboxError, SandboxPlan, ValidationExecutionBackend,
)
from app.services.native_test_generation import recheck_native_suite
from app.services.regression_protection import protect_regression_paths
from app.services.sandbox_dependencies import configured_bundle, DependencyPolicyError, verify_project_dependencies
from app.services.snapshot_service import load_snapshot, restore_snapshot
from app.services.solution_changes import apply_changes, resolve_safe_workspace_path

GATE_ORDER: tuple[GateName, ...] = ("baseline", "reference", "feature", "regression", "negative_control")
_BLOCKERS = {
    "infrastructure_unavailable", "busy", "unsupported", "dependency_policy", "dependency_installation",
    "timeout", "resource_failure", "invalid_evidence", "no_tests",
}
_EVIDENCE_QUOTA = 128 * 1024 * 1024
_MAX_REPORT_BYTES = 8 * 1024 * 1024


def _fingerprint(model) -> str:
    data = json.dumps(model.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def report_root() -> Path:
    configured = os.getenv("CTF_VALIDATION_REPORT_DIR")
    if configured:
        return Path(configured).absolute()
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CodingTaskFactory" / "validation"
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "coding-task-factory" / "validation"


@contextmanager
def _job_slot(root: Path, *, quota=_EVIDENCE_QUOTA) -> Iterator[None]:
    """A process-level advisory lock also covers multiple local API workers."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or bool(getattr(root.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise SandboxError("security_policy", "The operator evidence directory must not be a link.")
    lock_path = root / ".validation.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or lock_path.is_symlink():
            raise SandboxError("security_policy", "The validation lock is not an ordinary file.")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SandboxError("busy", "Another validation job is already running.") from exc
        if quota is not None and sum(item.stat().st_size for item in root.glob("*/*") if item.is_file()) >= quota:
            raise SandboxError("resource_failure", "The local evidence directory reached its retention quota.")
        yield


def new_report(request: ValidationRequest, snapshot=None) -> ValidationReport:
    return ValidationReport(
        id=str(uuid.uuid4()), task_id=request.task.id, solution_id=request.solution.id,
        test_suite_id=request.test_suite.id, execution_mode=request.execution_mode,
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        snapshot_sha256=snapshot.content_sha256 if snapshot else None,
        starting_commit_sha=snapshot.commit_sha if snapshot else (
            request.starting_commit_sha or request.task.starting_commit_sha or request.test_suite.repository.starting_commit_sha
        ),
        task_sha256=_fingerprint(request.task), solution_sha256=_fingerprint(request.solution),
        test_suite_sha256=_fingerprint(request.test_suite),
        overall_verdict="NOT_VALIDATED", gates=[GateEvidence(gate=name) for name in GATE_ORDER],
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _fail(gate: GateEvidence, category: FailureCategory, detail: str, *, ran: bool = True) -> None:
    gate.status = "failed" if ran else "not_run"
    gate.failure_category = category
    gate.detail = detail


def _positive(run: SandboxRun, targets: tuple[str, ...], selectors: tuple[tuple[str, str], ...]) -> FailureCategory | None:
    if run.timed_out:
        return "timeout"
    if run.oom_killed:
        return "resource_failure"
    if run.failure_category:
        return run.failure_category
    if run.exit_code is None or not run.commands:
        return "invalid_evidence"
    if run.exit_code != 0:
        return "test_error"
    if targets:
        if not run.cases:
            return "no_tests"
        actual = {(item.path, item.selector) for item in run.cases}
        found = {item.path for item in run.cases}
        if found != set(targets) or (selectors and actual != set(selectors)):
            return "invalid_evidence"
        if any(item.outcome == "skipped" for item in run.cases):
            return "test_skipped"
        if any(item.outcome != "passed" for item in run.cases):
            return "test_error"
    return None


def _apply(root: Path, request: ValidationRequest, analysis: RepoAnalysisResponse) -> list:
    files = request.solution.files
    protect_regression_paths(root, analysis.existing_test_files, [file.path for file in files],
                             {file.path: file.code for file in files})
    if not files or any(
        (file.status != "create" and not file.base_sha256)
        or (file.status != "delete" and not file.result_sha256)
        for file in files
    ):
        raise ValueError("This solution requires a Phase 2 change set with complete fingerprints.")
    return apply_changes(root, files, authorized_deletions=frozenset(request.authorized_deletions),
                         check_content=False)


def _mutations(request: ValidationRequest) -> Iterator[tuple[str, int, bytes]]:
    """Make up to a few controlled wrong-return variants without executing source."""
    for file in request.solution.files:
        if file.status == "delete" or not file.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(file.code)
        except (SyntaxError, ValueError):
            continue
        lines = file.code.encode().splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        for node in sorted((item for item in ast.walk(tree) if isinstance(item, ast.Return) and item.value is not None),
                           key=lambda item: (item.lineno, item.col_offset)):
            value = node.value
            literal = value.value if isinstance(value, ast.Constant) else None
            if isinstance(literal, bool):
                replacement = b"False" if literal else b"True"
            elif isinstance(literal, (int, float)):
                replacement = repr(literal + 1).encode()
            elif isinstance(literal, str):
                replacement = b"'_ctf_wrong_value_'"
            else:
                replacement = b"False" if isinstance(value, ast.Constant) else b"None"
            start = offsets[value.lineno - 1] + value.col_offset
            end = offsets[value.end_lineno - 1] + value.end_col_offset
            source = file.code.encode()
            mutated = source[:start] + replacement + source[end:]
            if mutated != source:
                yield file.path, node.lineno, mutated


class _Job:
    def __init__(self, request, snapshot, report, backend, directory, prepare_tests: Callable):
        self.request = request
        self.snapshot = snapshot
        self.report = report
        self.backend = backend
        self.directory = directory
        self.prepare_tests = prepare_tests
        self.gates = {gate.gate: gate for gate in report.gates}
        self.current = self.gates["baseline"]

    def run(self, root, plan, *, variant=None):
        path = self.directory / f"{self.current.gate}-{len(self.current.runs) + 1}.log"
        run = self.backend.run(root, plan, self.bundle, path)
        run.working_directory = plan.cwd
        run.operation = plan.kind
        run.framework = plan.framework
        run.targets = list(plan.targets)
        run.variant = variant
        self.current.runs.append(run)
        return run

    def positive(self, name, root, targets=(), selectors=()):
        self.current = self.gates[name]
        plan = SandboxPlan("compile" if name == "reference" else "tests", self.cwd,
                           self.project.framework, targets, selectors)
        run = self.run(root, plan)
        failure = _positive(run, targets, selectors)
        if failure:
            if name == "reference" and failure == "test_error":
                failure = "build_failed"
            _fail(self.current, failure, f"The {name.replace('_', ' ')} command did not pass.")
            return False
        self.current.status = "passed"
        return True

    def execute(self):
        request = self.request
        suite = request.test_suite
        self.bundle = configured_bundle(os.getenv("CTF_VALIDATION_WHEELHOUSE"))
        self.report.dependency_lock_sha256 = self.bundle.digest
        with restore_snapshot(self.snapshot) as (base, record):
            self.project = recheck_native_suite(base, record.analysis, request.task, request.solution, suite)
            self.cwd = self.project.scope.as_posix()
            verify_project_dependencies((base / self.cwd).resolve(), self.project.framework, self.bundle)
            originals = tuple(file.path for file in self.project.original_tests)
            if not originals:
                _fail(self.current, "no_tests", "The selected project has no preserved baseline test modules.", ran=False)
                return
            if not self.positive("baseline", base, originals):
                return
            original_cases = Counter((case.path, case.native_id or case.selector) for case in self.current.runs[-1].cases)

        with restore_snapshot(self.snapshot) as (reference, record):
            self.current = self.gates["reference"]
            try:
                changes = _apply(reference, request, record.analysis)
            except (ValueError, OSError) as exc:
                _fail(self.current, "patch_conflict", str(exc))
                return
            self.report.solution_applied_successfully = True
            self.report.created_files = [file.path for file in changes if file.status == "create"]
            self.report.modified_files = [file.path for file in changes if file.status == "modify"]
            self.report.deleted_files = [file.path for file in changes if file.status == "delete"]
            verify_project_dependencies((reference / self.cwd).resolve(), self.project.framework, self.bundle)
            if not self.positive("reference", reference):
                return
            self.current = self.gates["feature"]
            self.prepare_tests(reference, suite)
            features = tuple(file.path for file in suite.feature_test_files)
            feature_cases = tuple((file.path, case.name) for file in suite.feature_test_files for case in file.test_cases)
            if not self.positive("feature", reference, features, feature_cases):
                return
            for file in (*suite.feature_test_files, *suite.regression_test_files):
                resolve_safe_workspace_path(reference, file.path).unlink()
            regressions = tuple(file.path for file in suite.regression_test_files)
            if not self.positive("regression", reference, originals):
                return
            reference_cases = Counter((case.path, case.native_id or case.selector) for case in self.current.runs[-1].cases)
            if reference_cases != original_cases:
                _fail(self.current, "test_error", "The collected original regression cases changed.")
                return
            if regressions:
                self.prepare_tests(reference, suite)
                regression_cases = tuple((file.path, case.name) for file in suite.regression_test_files for case in file.test_cases)
                if not self.positive("regression", reference, regressions, regression_cases):
                    return

        self.current = self.gates["negative_control"]
        untreated = {(file.path, case.name) for file in suite.feature_test_files for case in file.test_cases
                     if case.negative_control.strategy == "untreated_baseline"}
        pending = set(feature_cases) - untreated
        if untreated:
            with restore_snapshot(self.snapshot) as (negative, _):
                self.prepare_tests(negative, suite)
                detected, failure = self.negative(negative, untreated, "untreated_baseline")
            if failure or detected != untreated:
                _fail(self.current, failure or "negative_control_escaped",
                      "The untreated snapshot did not produce the required assertion failures.")
                return

        for index, (path, line, code) in enumerate(_mutations(request)):
            if not pending or index >= 3:
                break
            with restore_snapshot(self.snapshot) as (mutant, record):
                _apply(mutant, request, record.analysis)
                self.prepare_tests(mutant, suite)
                resolve_safe_workspace_path(mutant, path).write_bytes(code)
                detected, failure = self.negative(mutant, pending, f"wrong_return:{path}:{line}")
            if failure in _BLOCKERS:
                _fail(self.current, failure, "A mutated-reference trial lost required execution evidence.")
                return
            if failure is None:
                pending -= detected
        if pending or not self.current.runs:
            _fail(self.current, "negative_control_escaped", "Some feature cases did not detect the missing or mutated behavior.",
                  ran=bool(self.current.runs))
            return
        self.current.status = "passed"
        self.current.detail = "Every planned feature selector produced a qualifying negative-control assertion failure."

    def negative(self, root: Path, selected: set[tuple[str, str]], variant: str):
        targets = tuple(sorted({path for path, _ in selected}))
        run = self.run(root, SandboxPlan("tests", self.cwd, self.project.framework, targets, tuple(sorted(selected))),
                       variant=variant)
        if run.timed_out:
            return set(), "timeout"
        if run.oom_killed:
            return set(), "resource_failure"
        if run.failure_category not in (None, "assertion_failure"):
            return set(), run.failure_category
        if not run.commands or run.exit_code not in (0, 1):
            return set(), "invalid_evidence"
        actual = {(case.path, case.selector) for case in run.cases}
        if actual != selected:
            return set(), "invalid_evidence"
        detected = {
            pair for pair in selected
            if all(case.outcome == "assertion_failed" for case in run.cases if (case.path, case.selector) == pair)
        }
        if run.exit_code != 1 or run.failure_category != "assertion_failure":
            detected = set()
        return detected, None


def _summarize(report: ValidationReport) -> None:
    for gate, field, kind in (
        (report.gates[0], "baseline_result", "baseline"),
        (report.gates[2], "feature_tests_result", "feature"),
        (report.gates[3], "regression_tests_result", "regression"),
        (report.gates[4], "mutation_test_result", "mutation"),
    ):
        if gate.runs:
            run = next((item for item in gate.runs if item.failure_category), gate.runs[-1])
            setattr(report, field, TestExecutionResult(
                test_file="; ".join(run.targets), test_kind=kind,
                exit_code=run.exit_code if run.exit_code is not None else -1,
                duration_ms=sum(item.duration_ms for item in gate.runs),
                passed=gate.status == "passed",
                stderr=f"See {run.log_location}" if run.log_location else "",
                individual_cases=[case.model_dump() for item in gate.runs for case in item.cases],
            ))
    report.baseline_status = report.gates[0].status
    report.mutation_test_status = report.gates[4].status


def execute_validation(request: ValidationRequest, snapshot_id: str, prepare_tests: Callable,
                       backend: ValidationExecutionBackend | None = None) -> ValidationReport:
    started = time.monotonic()
    record = load_snapshot(snapshot_id)
    report = new_report(request, record.analysis)
    if (
        request.task.snapshot_id != snapshot_id or request.solution.snapshot_id != snapshot_id
        or request.test_suite.repository.snapshot_id != snapshot_id
        or request.test_suite.quality_status != "structurally_checked"
    ):
        report.failure_category = "artifact_integrity"
        report.rejection_reasons = ["Docker validation needs matching, snapshot-backed Phase 2 and Phase 3 artifacts."]
        return report
    image = os.getenv("CTF_VALIDATION_IMAGE")
    if backend is None and not image:
        report.failure_category = "infrastructure_unavailable"
        report.rejection_reasons = ["Docker validation requires an operator-approved local image."]
        return report

    directory = None
    try:
        with _job_slot(report_root()):
            directory = report_root() / report.id
            directory.mkdir(mode=0o700)
            report.evidence_location = f"{report.id}/report.json"
            with tempfile.TemporaryDirectory(prefix="ctf-docker-config-") as config:
                backend = backend or DockerExecutionBackend(image, report.id, Path(config))
                backend.probe()
                report.sandbox_image_id = backend.image_id
                job = _Job(request, snapshot_id, report, backend, directory, prepare_tests)
                try:
                    job.execute()
                except DependencyPolicyError as exc:
                    _fail(job.current, "dependency_policy", str(exc), ran=False)
                except SandboxError as exc:
                    _fail(job.current, exc.category, str(exc), ran=False)
                except ValueError as exc:
                    _fail(job.current, "artifact_integrity", str(exc), ran=False)
            failed = next((gate for gate in report.gates if gate.failure_category), None)
            if all(gate.status == "passed" and gate.runs for gate in report.gates):
                report.overall_verdict = "VALIDATED"
            elif failed:
                report.failure_category = failed.failure_category
                report.rejection_reasons = [failed.detail or "A required gate did not pass."]
                report.overall_verdict = "NOT_VALIDATED" if failed.failure_category in _BLOCKERS else "REJECTED"
            _summarize(report)
    except (SandboxError, OSError) as exc:
        report.overall_verdict = "NOT_VALIDATED"
        report.failure_category = exc.category if isinstance(exc, SandboxError) else "infrastructure_unavailable"
        report.rejection_reasons = [str(exc) if isinstance(exc, SandboxError) else "The local validation evidence is unavailable."]
    report.execution_duration_ms = round((time.monotonic() - started) * 1000)
    if directory:
        try:
            temporary = directory / "report.tmp"
            payload = report.model_dump_json(indent=2).encode("utf-8")
            if len(payload) > _MAX_REPORT_BYTES:
                raise OSError("The report exceeds the per-job evidence limit.")
            temporary.write_bytes(payload)
            temporary.replace(directory / "report.json")
        except OSError:
            report.overall_verdict = "NOT_VALIDATED"
            report.failure_category = "infrastructure_unavailable"
            report.evidence_location = None
            report.rejection_reasons = ["The validation report could not be persisted."]
    return report


def read_recorded_validation(validation_id: str, task, solution, suite) -> ValidationReport:
    """Resolve an ID against trusted local evidence and check the exact artifacts."""
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", validation_id):
        raise ValueError("Validated export requires server-recorded Docker evidence.")
    try:
        directory = report_root() / validation_id
        path = directory / "report.json"
        for entry in (directory.parent, directory, path):
            info = entry.lstat()
            if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_REPORT_BYTES or info.st_nlink != 1:
            raise ValueError
        with path.open("rb") as handle:
            recorded = ValidationReport.model_validate_json(handle.read(_MAX_REPORT_BYTES + 1))
        if (recorded.id != validation_id or recorded.execution_mode not in ("docker", "isolated")
                or recorded.task_id != task.id or recorded.solution_id != solution.id or recorded.test_suite_id != suite.id
                or recorded.task_sha256 != _fingerprint(task)
                or recorded.solution_sha256 != _fingerprint(solution)
                or recorded.test_suite_sha256 != _fingerprint(suite)):
            raise ValueError
        if recorded.overall_verdict == "VALIDATED" and (
            [gate.gate for gate in recorded.gates] != list(GATE_ORDER)
            or any(gate.status != "passed" or not gate.runs for gate in recorded.gates)
        ):
            raise ValueError
        return recorded
    except (OSError, ValueError) as exc:
        raise ValueError("Validated export requires matching server-recorded Docker evidence.") from exc


def require_recorded_validation(report: ValidationReport, task, solution, suite) -> None:
    """Validate an existing report object's exact identity."""
    if report.overall_verdict == "VALIDATED" and read_recorded_validation(report.id, task, solution, suite) != report:
        raise ValueError("Validated export requires matching server-recorded Docker evidence.")
