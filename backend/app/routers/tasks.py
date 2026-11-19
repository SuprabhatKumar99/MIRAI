import tempfile
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from app.core.access import principal, shared
from app.core.gemini_errors import gemini_failure

from app.schemas.harbor import HarborExportRequest
from app.schemas.repo import RepoAnalysisRequest, RepoAnalysisResponse
from app.schemas.solution import SolutionRequest, SolutionResponse
from app.schemas.task import (
    GenerateFromGithubRequest,
    GenerateFromRepoRequest,
    RepoTaskRequest,
    TaskRequest,
    TaskResponse,
)
from app.schemas.test_suite import TestSuiteRequest, TestSuiteResponse
from app.schemas.workflow import TaskList
from app.schemas.validation import ValidationReport, ValidationRequest
from app.services import (
    harbor_service,
    repo_service,
    snapshot_service,
    solution_service,
    task_service,
    test_service,
    validation_service,
    workflow_store as store,
)

router = APIRouter(prefix="/tasks", tags=["Tasks"])


def _snapshot_http_error(error: snapshot_service.SnapshotError) -> HTTPException:
    status_by_error = {
        snapshot_service.SnapshotNotFound: 404,
        snapshot_service.SnapshotIntegrityError: 409,
        snapshot_service.SnapshotStorageError: 503,
    }
    return HTTPException(status_code=status_by_error.get(type(error), 400), detail=str(error))


# ── Topic-based Task Endpoints ──────────────────────────────────────

@router.get("/", response_model=TaskList)
def list_tasks(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
               language: str | None = Query(None, max_length=40), category: str | None = Query(None, max_length=60),
               difficulty: str | None = Query(None, max_length=20), project_id: str | None = Query(None, max_length=40),
               q: str | None = Query(None, max_length=120)):
    return store.list_tasks(principal(), limit, offset, language=language, category=category,
                            difficulty=difficulty, project_id=project_id, query=q)


def _save_task(task):
    analysis = snapshot_service.load_snapshot(task.snapshot_id).analysis if task.snapshot_id else None
    return store.save_task(principal(), task, analysis)


def _current(task):
    row = store.find_task(principal(), task.id)
    if row is None and shared():
        raise HTTPException(404, "The requested task was not found.")
    if row is not None and row["body"] != store.dump(task):
        raise HTTPException(409, "Use the server's saved task specification.")
    return row


def _parents(row, solution, suite=None):
    if row is None:
        return
    for kind, key, value in (("solution", "solution_id", solution), ("tests", "suite_id", suite)):
        if value is None:
            continue
        if row[key] != value.id or store.artifact(principal(), value.id, kind)["body"] != store.dump(value):
            raise HTTPException(409, "Use the task's current saved artifacts.")


def _legacy_write():
    if shared():
        raise HTTPException(409, "Use the tracked task workflow for this operation.")


def _provider_http_error(error):
    status, message = gemini_failure(error) or (502, _provider_error(error))
    return HTTPException(status_code=status, detail=message)


def _provider_error(error):
    message = str(error)
    if message.startswith("Gemini API request failed"):
        return "Gemini API request failed."
    if "not valid JSON" in message:
        return "Gemini returned content that is not valid JSON."
    if "missing expected fields" in message:
        return "Gemini response is missing expected fields."
    if "oversized" in message:
        return "Gemini returned missing or oversized content."
    if "must map to" in message:
        return "Generated changes must map to known requirements and acceptance criteria."
    if "starting test harness" in message:
        return "Reference changes cannot modify the starting test harness."
    if "mutated-reference negative control" in message:
        return "Feature-only imports require a mutated-reference negative control."
    return "The provider could not complete the operation."


@router.post("/generate", response_model=TaskResponse, status_code=201)
def generate_task(request: TaskRequest):
    """Generate a new software engineering task via Gemini AI from a topic."""
    try:
        return _save_task(task_service.generate_task(request))
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        raise _provider_http_error(e) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during task generation.")


# ── Repository Analysis Endpoints ───────────────────────────────────

@router.post("/analyze/github", response_model=RepoAnalysisResponse, status_code=200)
def analyze_github(request: RepoAnalysisRequest):
    """
    Download and analyze a public GitHub repository.

    Performs strict SSRF verification, safe zip extraction, and recursive codebase analysis.
    """
    try:
        options = {"revision": request.commit_sha} if request.commit_sha else {}
        return store.save_snapshot(principal(), repo_service.analyze_github_repository(str(request.repo_url), **options))
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except repo_service.GitHubRateLimitError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during repository analysis.")


@router.post("/analyze/upload", response_model=RepoAnalysisResponse, status_code=200)
def analyze_upload(file: UploadFile = File(...)):
    """
    Analyze an uploaded repository ZIP file.

    Performs structural validation, magic byte verification, Zip-Slip prevention,
    and recursive codebase analysis.
    """
    filename = file.filename or "upload.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400, detail="Only .zip archive uploads are supported."
        )

    try:
        return store.save_snapshot(principal(), repo_service.analyze_uploaded_zip(file.file, filename=filename))
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during repository analysis.")


# ── Contextual Repository Task Generation Endpoints ─────────────────

@router.post("/generate/from-repo", response_model=TaskResponse, status_code=201)
def generate_task_from_analysis(request: GenerateFromRepoRequest):
    """
    Generate a contextual coding task from an existing repository analysis.
    """
    reference = request.snapshot_id or (request.repo_analysis.snapshot_id if request.repo_analysis else None)
    if shared() and (not reference or not store.has_snapshot(principal(), reference)):
        raise HTTPException(404, "The source snapshot was not found.")
    try:
        return _save_task(task_service.generate_task_from_repo(
            repo_analysis=request.repo_analysis,
            options=request.options,
            snapshot_id=request.snapshot_id,
        ))
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        raise _provider_http_error(e) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during task generation.")


@router.post("/generate/github", response_model=TaskResponse, status_code=201)
def generate_task_from_github(request: GenerateFromGithubRequest):
    """
    Ingest, analyze, and generate a contextual coding task from a public GitHub repository in one call.
    """
    try:
        # Step 1: Ingest & analyze
        analysis = repo_service.analyze_github_repository(str(request.repo_url))
        # Step 2: Generate task
        store.save_snapshot(principal(), analysis)
        return _save_task(task_service.generate_task_from_repo(
            repo_analysis=analysis,
            options=request.options,
        ))
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except repo_service.GitHubRateLimitError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except ValueError as e:
        if "GEMINI_API_KEY" in str(e):
            raise HTTPException(status_code=503, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        if failure := gemini_failure(e):
            raise HTTPException(status_code=failure[0], detail=failure[1]) from e
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during task generation.")


@router.post("/generate/upload", response_model=TaskResponse, status_code=201)
def generate_task_from_upload(
    file: UploadFile = File(...),
    difficulty: Literal["easy", "medium", "hard"] = Form("medium"),
    focus_area: str | None = Form(None),
    category: str | None = Form(None),
):
    """
    Upload, analyze, and generate a contextual coding task from a ZIP archive in one call.
    """
    filename = file.filename or "upload.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400, detail="Only .zip archive uploads are supported."
        )

    options = RepoTaskRequest(
        difficulty=difficulty,
        focus_area=focus_area,
        category=category,
    )

    try:
        # Step 1: Ingest & analyze
        analysis = repo_service.analyze_uploaded_zip(file.file, filename=filename)
        # Step 2: Generate task
        store.save_snapshot(principal(), analysis)
        return _save_task(task_service.generate_task_from_repo(
            repo_analysis=analysis,
            options=options,
        ))
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except ValueError as e:
        if "GEMINI_API_KEY" in str(e):
            raise HTTPException(status_code=503, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        if failure := gemini_failure(e):
            raise HTTPException(status_code=failure[0], detail=failure[1]) from e
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during task generation.")


# ── Reference Solution Endpoint ─────────────────────────────────────

@router.post("/solution", response_model=SolutionResponse, status_code=201)
def generate_solution(request: SolutionRequest):
    """Generate a reference candidate and check application when a snapshot is available."""
    _legacy_write()
    row = _current(request.task)
    try:
        result = solution_service.generate_reference_solution(
            request.task, authorized_deletions=request.authorized_deletions
        )
        return store.attach_legacy(principal(), request.task, result, "solution", row["revision"]) if row else result
    except store.WorkflowConflict:
        raise
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except solution_service.SolutionInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        raise _provider_http_error(e) from e
    except Exception:
        raise HTTPException(status_code=500, detail="Reference solution generation failed.")


# ── Automated Test Suite Endpoint ───────────────────────────────────

@router.post("/tests", response_model=TestSuiteResponse, status_code=201)
def generate_test_suite(request: TestSuiteRequest):
    """Generate native Python tests and an isolated execution plan from a snapshot."""
    _legacy_write()
    row = _current(request.task)
    _parents(row, request.solution)
    try:
        result = test_service.generate_test_suite(
            task=request.task,
            solution=request.solution,
            repository_context=request.repo_analysis,
            framework=request.preferred_framework,
            starting_commit_sha=request.starting_commit_sha,
        )
        return store.attach_legacy(principal(), request.task, result, "tests", row["revision"]) if row and request.solution else result
    except store.WorkflowConflict:
        raise
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except test_service.TestGenerationInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        raise _provider_http_error(e) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during test generation.")


# ── Validation Engine Endpoint ──────────────────────────────────────

@router.post("/validate", response_model=ValidationReport, status_code=200)
def validate_task_package(request: ValidationRequest):
    """Run validation against the verified, immutable source snapshot."""
    _legacy_write()
    row = _current(request.task)
    _parents(row, request.solution, request.test_suite)
    try:
        result = validation_service.validate_task_package(workspace_root=None, request=request)
        return store.attach_legacy(principal(), request.task, result, "validation", row["revision"]) if row else result
    except store.WorkflowConflict:
        raise
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error during validation.") from e


# ── Harbor Task Package Export Endpoint ─────────────────────────────

@router.post("/export/harbor", status_code=200)
def export_harbor_task(request: HarborExportRequest):
    """Export a snapshot-backed task with a separate verifier and a checksum-checked oracle."""
    _legacy_write()
    _parents(_current(request.task), request.solution, request.test_suite)
    try:
        with tempfile.TemporaryDirectory(prefix="ctf_harbor_export_") as tmp_dir:
            zip_path, report = harbor_service.export_harbor_task_package(
                request, output_dir=tmp_dir
            )
            zip_bytes = zip_path.read_bytes()

        safe_filename = f"{harbor_service.sanitize_task_dirname(request.task_prefix, request.task.id)}.zip"

        headers = {
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Length": str(len(zip_bytes)),
            "X-Harbor-Task-Name": report.harbor_task_name,
            "X-Validation-Status": report.validation_status_recorded,
        }
        if report.snapshot_id:
            headers["X-Repository-Snapshot-Id"] = report.snapshot_id
        if report.validation_id:
            headers["X-Validation-Id"] = report.validation_id
        return Response(content=zip_bytes, media_type="application/zip", headers=headers)
    except snapshot_service.SnapshotError as e:
        raise _snapshot_http_error(e) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Harbor export failed due to an unexpected server error.",
        )
