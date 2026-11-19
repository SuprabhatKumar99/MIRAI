"""Run a local oracle-gated evaluation campaign against a single immutable package."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tomllib
from uuid import uuid4

from app.schemas.evaluation import EvaluationReport, EvaluationRequest, PackageEvidence, TrialEvidence
from app.services.docker_sandbox import SandboxError
from app.services.evaluation_metrics import populate_metrics
from app.services.harbor_evaluation import AGENT_SOURCE, HarborEvaluationBackend, RunnerBlocked
from app.services.harbor_guest import digest, ordinary
from app.services.harbor_service import PYTHON_IMAGE, export_harbor_task_package
from app.services.isolated_validation import _job_slot
from app.services.snapshot_service import SnapshotError

STORAGE_LIMIT = 1024 * 1024 * 1024
REPORT_LIMIT = 5 * 1024 * 1024


def evaluation_root():
    if configured := os.getenv("CTF_EVALUATION_DIR"):
        return Path(configured).absolute()
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData/Local") / "CodingTaskFactory/evaluations"
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local/share") / "coding-task-factory/evaluations"


def _save(directory, report):
    data = report.model_dump_json(indent=2).encode()
    if len(data) > REPORT_LIMIT:
        raise OSError("Evaluation report exceeds the evidence limit.")
    temporary = directory / "report.next"
    with temporary.open("wb") as output:
        output.write(data)
    temporary.replace(directory / "report.json")


def _directory(run_id):
    if not re.fullmatch("[0-9a-f]{32}", run_id):
        raise FileNotFoundError("Evaluation not found.")
    root = evaluation_root()
    ordinary(root, directory=True)
    directory = root / run_id
    ordinary(directory, directory=True)
    return directory


def read_evaluation(run_id):
    path = _directory(run_id) / "report.json"
    ordinary(path)
    with path.open("rb") as source:
        data = source.read(REPORT_LIMIT + 1)
    if len(data) > REPORT_LIMIT:
        raise ValueError("Evaluation report exceeds its size limit.")
    report = EvaluationReport.model_validate_json(data)
    if report.id != run_id:
        raise ValueError("Evaluation record identity mismatch.")
    return report


def create_evaluation(request: EvaluationRequest):
    root = evaluation_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ordinary(root, directory=True)
    identifier = uuid4().hex
    directory = root / identifier
    directory.mkdir(mode=0o700)
    report = EvaluationReport(id=identifier, mode=request.mode, agents=request.agents, repeats=request.repeats,
                              task_id=request.package.task.id)
    _save(directory, report)
    return report


def _retained_bytes(root):
    total = 0
    for base, directories, names in os.walk(root):
        files = [Path(base) / name for name in names]
        directories[:] = [
            name for name in directories if not (Path(base) / name).is_symlink()
            and not getattr((Path(base) / name).lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ]
        for path in files:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            if total >= STORAGE_LIMIT:
                return total
    return total


def _tree_sha(root):
    """Hash staged bytes and layout before each execution."""
    ordinary(root, directory=True)
    entries = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        is_dir = stat.S_ISDIR(info.st_mode)
        ordinary(path, directory=is_dir)
        entries.append((path.relative_to(root).as_posix(), None if is_dir else digest(path)))
    data = json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _package(directory, request):
    archive, export = export_harbor_task_package(request.package, directory / "package")
    if export.validation_status_recorded == "REJECTED":
        raise RunnerBlocked("task_defect", "The supplier's recorded Phase 4 validation rejected this package.")
    task = request.package.task
    stage = Path(export.export_directory)
    config = tomllib.loads((stage / "task.toml").read_text(encoding="utf-8"))
    metadata = config["metadata"]
    manifest = json.loads((stage / "environment/snapshot.json").read_bytes())
    plan_path = stage / "tests/plan.json"
    plan = json.loads(plan_path.read_bytes())
    provenance = PackageEvidence(
        task_id=task.id, language=task.language, category=task.category, difficulty=task.difficulty,
        snapshot_id=export.snapshot_id, snapshot_sha256=export.snapshot_sha256, starting_commit_sha=manifest["commit_sha"],
        task_sha256=metadata["task_sha256"], solution_sha256=metadata["solution_sha256"],
        test_suite_sha256=metadata["test_suite_sha256"], dependency_lock_sha256=metadata["dependency_lock_sha256"],
        package_sha256=digest(archive), package_tree_sha256=_tree_sha(stage), verifier_plan_sha256=digest(plan_path),
        runtime_image=PYTHON_IMAGE, validation_id=export.validation_id,
        agent_timeout_seconds=int(config["agent"]["timeout_sec"] * 0.2),
        verifier_timeout_seconds=config["verifier"]["timeout_sec"], memory_mb=config["environment"]["memory_mb"],
        cpus=int(config["environment"]["cpus"]),
    )
    return stage, plan, manifest["files"], provenance


def _negative_control_passed(trial):
    regression = trial.regression_tests
    return (
        trial.trustworthy and trial.reward == 0 and trial.build_passed
        and trial.feature_failure_kind == "assertion_failure" and trial.feature_tests is not None
        and regression is not None and regression.passed == regression.total
    )


def _run_campaign(report, directory, request, backend):
    report.state = "running"
    _save(directory, report)
    report.harbor_version, report.docker_engine = backend.probe(directory, live=request.mode == "agent")
    report.agent_code_sha256 = digest(AGENT_SOURCE) if request.mode == "agent" else None
    try:
        stage, plan, originals, report.package = _package(directory, request)
    except (ValueError, SnapshotError) as exc:
        raise RunnerBlocked("task_admission", str(exc)) from exc
    _save(directory, report)

    def execute(role, label, attempt=1, configuration=None):
        if _retained_bytes(directory.parent) >= STORAGE_LIMIT:
            raise RunnerBlocked("environment_failure", "The evaluation store reached its retention limit.")
        if _tree_sha(stage) != report.package.package_tree_sha256:
            raise RunnerBlocked("invalid_evidence", "The immutable Harbor package changed during evaluation.")
        if role == "agent" and digest(AGENT_SOURCE) != report.agent_code_sha256:
            raise RunnerBlocked("invalid_evidence", "The agent adapter changed during evaluation.")
        trial = TrialEvidence(role=role, origin=backend.origin, label=label, attempt=attempt,
                              configuration=configuration)
        backend.run(stage, directory, trial, plan, originals)
        if _tree_sha(stage) != report.package.package_tree_sha256 or (
            role == "agent" and digest(AGENT_SOURCE) != report.agent_code_sha256
        ):
            trial.trustworthy = False
            trial.reward = None
            trial.failure = "invalid_evidence"
        if report.oracle and trial.harbor_task_checksum != report.oracle.harbor_task_checksum:
            trial.trustworthy = False
            trial.reward = None
            trial.failure = trial.failure or "invalid_evidence"
        if trial.scored and any(
            prior.scored and prior.label == trial.label and (
                prior.resolved_models != trial.resolved_models
                or prior.configuration.model != trial.configuration.model
            )
            for prior in report.trials
        ):
            trial.flags.append("provider_model_version_changed")
            trial.trustworthy = False
            trial.failure = "invalid_evidence"
        return trial

    report.oracle = execute("oracle", "reference")
    _save(directory, report)
    if not report.oracle.trustworthy:
        raise RunnerBlocked(report.oracle.failure or "invalid_evidence", "The oracle did not produce trustworthy evidence.")
    if report.oracle.reward != 1:
        raise RunnerBlocked("task_defect", "The reference solution failed its Harbor oracle trial.")

    report.baseline_control = execute("control", "untreated")
    _save(directory, report)
    control = report.baseline_control
    if not control.trustworthy:
        raise RunnerBlocked(control.failure or "invalid_evidence", "The untreated control did not produce trustworthy evidence.")
    if not _negative_control_passed(control):
        raise RunnerBlocked("task_defect", "The untreated snapshot failed the feature-only negative-control check.")

    if request.mode == "agent":
        # Interleave variants by repeat so each round uses equivalent task state and budgets.
        for attempt in range(1, request.repeats + 1):
            for configuration in request.agents:
                report.trials.append(execute("agent", configuration.label, attempt, configuration))
                populate_metrics(report)
                _save(directory, report)
        if not any(trial.trustworthy for trial in report.trials):
            failed = report.trials[0].failure or "invalid_evidence"
            diagnostics = [flag for trial in report.trials for flag in trial.flags if flag.startswith("Provider HTTP ")]
            detail = "No agent trial produced trustworthy verifier evidence."
            if diagnostics:
                detail += " " + diagnostics[0]
            raise RunnerBlocked(failed, detail)
    else:
        for attempt in range(2, request.repeats + 1):
            report.trials.append(execute("control", "untreated", attempt))
            _save(directory, report)
            if not _negative_control_passed(report.trials[-1]):
                raise RunnerBlocked("invalid_evidence", "Repeated untreated control produced inconsistent evidence.")
    report.state = "completed"


def run_evaluation(run_id, request, backend=None):
    """Execute a queued run. The API never constructs simulated evaluation evidence."""
    directory = _directory(run_id)
    report = read_evaluation(run_id)
    if report.state != "queued":
        return report
    backend = backend or HarborEvaluationBackend()
    try:
        with _job_slot(directory.parent, quota=None):
            _run_campaign(report, directory, request, backend)
    except RunnerBlocked as exc:
        report.failure, report.detail = exc.category, str(exc)
        report.state = "quarantined" if exc.category == "task_defect" else "blocked"
    except SandboxError as exc:
        report.failure = "busy" if exc.category == "busy" else "environment_failure"
        report.detail = "Another campaign is running." if report.failure == "busy" else "The local evidence store is unavailable."
        report.state = "blocked"
    except Exception:
        report.failure, report.detail = "environment_failure", "The local evaluation controller failed."
        report.state = "failed"
    finally:
        populate_metrics(report)
        report.finished_at = datetime.now(timezone.utc)
        _save(directory, report)
    return report
