"""Owner-scoped catalogue, job, and supported evidence endpoints."""

import json
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from app.core.access import COOKIE, principal, shared
from app.schemas.repo import RepoAnalysisResponse
from app.schemas.workflow import HistoryPage, JobRequest, JobResponse, Kind, ProjectList, TaskDetail
from app.services import evaluation_service, workflow_jobs
from app.services import workflow_store as store
from app.services.evaluation_metrics import summary_csv, trials_csv
from app.services.harbor_guest import ordinary
from app.services.isolated_validation import report_root

router = APIRouter(tags=["Workflow"])

def _owned(read, *args, **kwargs):
    try:
        return read(principal(), *args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "The requested record was not found.") from exc


def _job(value):
    return {key: value[key] for key in ("id", "task_id", "kind", "revision", "state", "cancel_requested",
                                       "result_id", "error", "created_at", "updated_at")}


@router.get("/auth/session")
def auth_mode():
    return {"required": shared()}


@router.post("/auth/session")
def sign_in(request: Request, response: Response):
    if shared():
        response.set_cookie(COOKIE, request.headers["authorization"].partition(" ")[2],
                            secure=True, httponly=True, samesite="strict", max_age=28800)
    return {"signed_in": True}


@router.delete("/auth/session")
def sign_out(response: Response):
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
    return {"signed_in": False}


@router.get("/projects", response_model=ProjectList)
def projects(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
    return {"projects": store.list_projects(principal(), limit, offset), "limit": limit, "offset": offset}


@router.get("/snapshots/{snapshot_id}", response_model=RepoAnalysisResponse)
def snapshot_detail(snapshot_id: str):
    return _owned(store.snapshot, snapshot_id)


@router.get("/tasks/{task_id}", response_model=TaskDetail)
def task_detail(task_id: str):
    return _owned(store.detail, task_id)


@router.get("/tasks/{task_id}/history/{kind}", response_model=HistoryPage)
def task_history(task_id: str, kind: Kind, limit: int = Query(20, ge=1, le=100),
                 offset: int = Query(0, ge=0, le=100000)):
    return {"items": _owned(store.history, task_id, kind, limit, offset), "limit": limit, "offset": offset}


@router.post("/tasks/{task_id}/jobs", status_code=202, response_model=JobResponse)
def queue_job(task_id: str, payload: JobRequest, response: Response, background: BackgroundTasks):
    if payload.agents:
        configured = os.getenv("CTF_AGENT_MODELS")
        allowed = {name.strip() for name in (configured or "").split(",") if name.strip()}
        if (shared() or configured is not None) and any(
            model not in allowed for agent in payload.agents for model in (agent.model, agent.fallback_model) if model
        ):
            raise HTTPException(403, "This model is not enabled for live evaluations.")
    params = payload.model_dump(mode="json", exclude={"kind", "revision", "request_key"})
    job = _owned(store.enqueue, task_id, payload.kind, payload.revision, params, payload.request_key)
    response.headers["Location"] = f"/jobs/{job['id']}"
    if job["state"] == "queued":
        background.add_task(workflow_jobs.drain_jobs)
    return _job(job)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def job_status(job_id: str):
    return _job(_owned(store.read_job, job_id))


@router.delete("/jobs/{job_id}", response_model=JobResponse)
def cancel_job(job_id: str):
    return _job(_owned(store.cancel, job_id))


@router.get("/history/{kind}/{record_id}")
def historical_artifact(kind: Kind, record_id: str):
    return json.loads(_owned(store.artifact, record_id, kind)["body"])


def _file(directory: Path, name: str, maximum: int):
    if Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", name) or name in {".", ".."}:
        raise HTTPException(404, "The requested evidence file was not found.")
    try:
        ordinary(directory, directory=True)
        path = directory / name
        if ordinary(path).st_size > maximum:
            raise HTTPException(413, "The evidence file exceeds the download limit.")
        return path
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "The requested evidence file was not found.") from exc


@router.get("/history/export/{record_id}/download")
def download_export(record_id: str):
    record = _owned(store.artifact, record_id, "export")
    if not re.fullmatch(r"[0-9a-f]{32}", record_id):
        raise HTTPException(404, "The requested package was not found.")
    ordinary_root = store.state_root() / "exports"
    try:
        ordinary(ordinary_root, directory=True)
        path = _file(ordinary_root / record_id, record["locator"], 224 * 1024 * 1024)
        if store.file_sha256(path) != record["sha256"]:
            raise HTTPException(409, "The saved package failed its integrity check.")
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "The requested package was not found.") from exc
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.get("/history/validation/{record_id}/logs/{name}")
def validation_log(record_id: str, name: str):
    body = json.loads(_owned(store.artifact, record_id, "validation")["body"])
    expected = f"{record_id}/{name}"
    allowed = {run["log_location"] for gate in body["gates"] for run in gate["runs"] if run["log_location"]}
    if expected not in allowed or not re.fullmatch(r"[0-9a-f-]{36}", record_id):
        raise HTTPException(404, "The requested log was not found.")
    try:
        ordinary(report_root(), directory=True)
        path = _file(report_root() / record_id, name, 128 * 1024)
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "The requested log was not found.") from exc


@router.get("/history/evaluation/{record_id}/logs/{name}")
def evaluation_log(record_id: str, name: str):
    body = json.loads(_owned(store.artifact, record_id, "evaluation")["body"])
    trials = [body.get("oracle"), body.get("baseline_control"), *body.get("trials", [])]
    allowed = {trial["process"]["console_log"] for trial in trials if trial and trial["process"]}
    if name not in allowed or not re.fullmatch(r"[0-9a-f]{32}", record_id):
        raise HTTPException(404, "The requested log was not found.")
    try:
        ordinary(evaluation_service.evaluation_root(), directory=True)
        path = _file(evaluation_service.evaluation_root() / record_id, name, 1024 * 1024)
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "The requested log was not found.") from exc


@router.get("/history/evaluation/{record_id}/{format_name}")
def evaluation_csv(record_id: str, format_name: Literal["summary.csv", "trials.csv"]):
    record = _owned(store.artifact, record_id, "evaluation")
    report = evaluation_service.EvaluationReport.model_validate_json(record["body"])
    data = summary_csv(report) if format_name == "summary.csv" else trials_csv(report)
    return Response(data, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{format_name}"'})
