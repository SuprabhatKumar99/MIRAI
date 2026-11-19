"""Prepare verified workspaces and dispatch Docker or static validation."""

import hashlib
from pathlib import Path
import time
import unicodedata

from app.schemas.repo import RepoAnalysisResponse
from app.schemas.solution import SolutionResponse
from app.schemas.test_suite import TestSuiteResponse
from app.schemas.validation import ValidationReport, ValidationRequest
from app.services.docker_sandbox import ValidationExecutionBackend
from app.services.isolated_validation import execute_validation, new_report
from app.services.solution_changes import apply_changes, check_change_paths, resolve_safe_workspace_path
from app.services.regression_protection import protect_regression_paths
from app.services.snapshot_service import matching_snapshot_id, restore_snapshot


def apply_solution_safely(
    workspace_root: Path | str,
    solution: SolutionResponse,
) -> tuple[list[str], list[str]]:
    """Apply existing create/modify callers' changes on a controlled copy."""
    files = apply_changes(workspace_root, solution.files)
    return (
        [file.path for file in files if file.status == "create"],
        [file.path for file in files if file.status == "modify"],
    )


# ── Safe Test File Preparation (Non-Executing) ──────────────────────

def prepare_test_files_safely(
    workspace_root: Path | str,
    test_suite: TestSuiteResponse,
) -> tuple[list[str], list[str]]:
    """Verify originals and preflight generated test files before writing exact bytes."""
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise ValueError("Workspace root must be an existing directory.")

    for original in test_suite.original_regression_files:
        target = resolve_safe_workspace_path(root, original.path)
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != original.sha256:
            raise ValueError("An original regression file is missing or its checksum changed.")

    staged: list[tuple[Path, bytes]] = []
    seen: set[tuple[str, ...]] = set()
    paths: dict[str, list[str]] = {"feature": [], "regression": []}
    for kind, files in (("feature", test_suite.feature_test_files), ("regression", test_suite.regression_test_files)):
        for file in files:
            if file.test_kind != kind:
                raise ValueError("Test file group does not match its declared kind.")
            target = resolve_safe_workspace_path(root, file.path)
            key = tuple(unicodedata.normalize("NFC", part).casefold() for part in target.relative_to(root).parts)
            if key in seen:
                raise ValueError("Duplicate test file path.")
            if target.exists():
                raise ValueError(f"{kind.title()} test file collides with existing repository file.")
            code = file.code.encode("utf-8")
            if file.content_sha256 and hashlib.sha256(code).hexdigest() != file.content_sha256:
                raise ValueError("Generated test content checksum does not match.")
            seen.add(key)
            staged.append((target, code))
            paths[kind].append(file.path)
    if any(path[:length] in seen for path in seen for length in range(1, len(path))):
        raise ValueError("Generated test paths conflict with a parent path.")

    for target, code in staged:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(code)
    return paths["feature"], paths["regression"]


def validate_task_package(
    workspace_root: Path | str | None,
    request: ValidationRequest,
    backend: ValidationExecutionBackend | None = None,
) -> ValidationReport:
    """Evaluate the actual snapshot. Explicit workspaces support internal static callers."""
    snapshot_id = matching_snapshot_id(
        request.task.snapshot_id, request.solution.snapshot_id, request.test_suite.repository.snapshot_id
    )
    if request.execution_mode in {"docker", "isolated"} and snapshot_id is not None:
        return execute_validation(request, snapshot_id, prepare_test_files_safely, backend)
    if snapshot_id is not None:
        with restore_snapshot(snapshot_id) as (root, record):
            return _validate_workspace(root, request, record.analysis)
    if workspace_root is not None and request.execution_mode not in {"docker", "isolated"}:
        return _validate_workspace(Path(workspace_root).resolve(), request)
    report = new_report(request)
    report.failure_category = "artifact_integrity"
    try:
        check_change_paths(request.solution.files)
    except ValueError as exc:
        report.overall_verdict = "REJECTED"
        report.rejection_reasons = [f"Solution Application Integrity Failed: {exc}"]
    else:
        report.rejection_reasons = ["Restore an immutable repository snapshot before validation."]
    return report


def _validate_workspace(
    root: Path,
    request: ValidationRequest,
    snapshot: RepoAnalysisResponse | None = None,
) -> ValidationReport:
    start_time = time.monotonic()
    rejection_reasons: list[str] = []

    # ── Gate 2: Solution Application Integrity ─────────────────────
    created_files: list[str] = []
    modified_files: list[str] = []
    deleted_files: list[str] = []
    solution_applied_ok = False

    try:
        if snapshot:
            protect_regression_paths(root, snapshot.existing_test_files, [file.path for file in request.solution.files],
                                     {file.path: file.code for file in request.solution.files})
        if any(file.status == "delete" and file.base_sha256 is None for file in request.solution.files):
            raise ValueError("A deletion requires a verified base checksum.")
        files = apply_changes(
            root,
            request.solution.files,
            authorized_deletions=frozenset(request.authorized_deletions) if snapshot else frozenset(),
            check_content=bool(snapshot and request.solution.structural_check),
        )
        created_files = [file.path for file in files if file.status == "create"]
        modified_files = [file.path for file in files if file.status == "modify"]
        deleted_files = [file.path for file in files if file.status == "delete"]
        solution_applied_ok = True
    except ValueError as exc:
        rejection_reasons.append(f"Solution Application Integrity Failed: {exc}")

    # ── Prepare Test Files ─────────────────────────────────────────
    feature_paths: list[str] = []
    regression_paths: list[str] = []
    test_files_ready = False

    if solution_applied_ok:
        try:
            feature_paths, regression_paths = prepare_test_files_safely(root, request.test_suite)
            test_files_ready = True
        except ValueError as exc:
            rejection_reasons.append(f"Test File Preparation Failed: {exc}")

    # ── Gate 1, 3, 4, 5: Execution Handling ────────────────────────
    # In static mode or without container isolation, we do not execute untrusted code.
    if not solution_applied_ok or not test_files_ready:
        verdict = "REJECTED"
    else:
        verdict = "NOT_VALIDATED"
        rejection_reasons.append(
            "This mode performs static checks only. Use Docker mode to run the validation gates."
        )

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    report = new_report(request, snapshot)
    report.solution_applied_successfully = solution_applied_ok
    report.created_files = created_files
    report.modified_files = modified_files
    report.deleted_files = deleted_files
    report.overall_verdict = verdict
    report.failure_category = "artifact_integrity" if verdict == "REJECTED" else None
    report.rejection_reasons = rejection_reasons
    report.execution_duration_ms = elapsed_ms
    return report
