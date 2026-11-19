"""Run durable workflow operations against server-owned artifact versions."""

import json
import tempfile

from app.core.access import reserve_model
from app.core.gemini_errors import gemini_failure
from app.schemas.evaluation import EvaluationRequest
from app.schemas.harbor import HarborExportRequest
from app.schemas.solution import SolutionResponse
from app.schemas.task import TaskResponse
from app.schemas.test_suite import TestSuiteResponse
from app.schemas.validation import ValidationRequest
from app.services import evaluation_service, harbor_service, snapshot_service, solution_service, test_service, validation_service
from app.services import workflow_store as store
from app.services.docker_sandbox import SandboxError
from app.services.isolated_validation import _job_slot, require_recorded_validation


def _payload(snapshot):
    task = TaskResponse.model_validate(snapshot["task"])
    solution = SolutionResponse.model_validate(snapshot["solution"])
    suite = TestSuiteResponse.model_validate(snapshot["test_suite"])
    return task, solution, suite


def _package(task, solution, suite, snapshot, prefix):
    report = snapshot["validation"]
    return HarborExportRequest(
        task=task, solution=solution, test_suite=suite, task_prefix=prefix,
        validation_id=report["id"] if report and report["overall_verdict"] == "VALIDATED" else None,
    )


def _run(job):
    owner, task_id, kind = (job[key] for key in ("owner", "task_id", "kind"))
    snapshot = store.detail(owner, task_id)
    if snapshot["revision"] != job["revision"]:
        raise store.WorkflowConflict("This task has a newer artifact revision.")
    params = json.loads(job["parameters"])
    job_result = None
    if kind in {"solution", "tests", "evaluation"}:
        amount = 2
        if kind == "evaluation":
            amount = params["repeats"] * sum(agent["max_turns"] for agent in params["agents"])
        if amount and not reserve_model(owner, amount):
            raise store.WorkflowConflict("Daily model request budget reached.")
    task = TaskResponse.model_validate(snapshot["task"])
    if kind == "solution":
        job_result = solution_service.generate_reference_solution(task)
    elif kind == "tests":
        job_result = test_service.generate_test_suite(
            task=task, solution=SolutionResponse.model_validate(snapshot["solution"]),
        )
    else:
        task, solution, suite = _payload(snapshot)
        if kind == "validation":
            payload = ValidationRequest(task=task, solution=solution, test_suite=suite, execution_mode="docker")
            job_result = validation_service.validate_task_package(None, payload)
            require_recorded_validation(job_result, task, solution, suite)
        else:
            payload = _package(task, solution, suite, snapshot, params.get("task_prefix", "ctf"))
            if kind == "evaluation":
                request = EvaluationRequest(package=payload, **{key: params[key] for key in ("mode", "agents", "repeats")})
                report = evaluation_service.create_evaluation(request)
                job_result = evaluation_service.run_evaluation(report.id, request)
            elif kind == "export":
                target = store.state_root() / "exports" / job["id"]
                target.mkdir(parents=True, exist_ok=False, mode=0o700)
                with tempfile.TemporaryDirectory(dir=target) as scratch:
                    archive, report = harbor_service.export_harbor_task_package(payload, output_dir=scratch)
                    checksum = store.file_sha256(archive)
                    filename = archive.name
                    archive.replace(target / filename)
                body = report.model_dump(mode="json", exclude={"export_directory", "archive_path"})
                body.update(id=job["id"], filename=filename, sha256=checksum)
                return job["id"], body, filename, checksum
    return job_result.id, job_result.model_dump(mode="json"), None, None


def drain_jobs():
    """Only a process holding the worker lock can recover or execute catalogue jobs."""
    try:
        with _job_slot(store.state_root(), quota=None):
            store.recover_running()
            while job := store.claim():
                try:
                    result = _run(job)
                    store.finish(job, result)
                except store.WorkflowConflict as exc:
                    store.finish(job, error=str(exc))
                except snapshot_service.SnapshotNotFound:
                    store.finish(job, error="The source snapshot is missing. Re-upload the original ZIP or analyze the same GitHub commit, then retry.")
                except snapshot_service.SnapshotIntegrityError:
                    store.finish(job, error="The source snapshot failed its integrity check. Restore it from a trusted backup or create a task from a fresh snapshot.")
                except snapshot_service.SnapshotStorageError:
                    store.finish(job, error="The snapshot storage is unavailable. Check the backend's snapshot directory and permissions.")
                except (ValueError, RuntimeError) as exc:
                    message = str(exc)
                    if failure := gemini_failure(exc):
                        message = failure[1]
                    elif "GEMINI_API_KEY" in message:
                        message = "Configure Gemini credentials on the backend."
                    elif message == "Validation used a different runtime or dependency lock. Revalidate for this package.":
                        pass
                    elif job["kind"] in {"solution", "tests"}:
                        message = "Artifact generation could not finish. Review the task and provider configuration."
                    else:
                        message = "The operation could not finish. Check its prerequisites and local infrastructure."
                    store.finish(job, error=message)
                except Exception:
                    store.finish(job, error="The local workflow worker could not complete this operation.")
    except SandboxError:
        return
