"""Local operator API for evaluation jobs and summaries."""

from ipaddress import ip_address

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.schemas.evaluation import EvaluationReport, EvaluationRequest
from app.core.access import reserve_model, shared
from app.services import evaluation_service
from app.services.evaluation_metrics import summary_csv, trials_csv

router = APIRouter(prefix="/evaluations", tags=["Evaluations"])


def _require_local(request: Request):
    if shared():
        raise HTTPException(409, "Use the task's tracked evaluation workflow.")
    try:
        if request.client and ip_address(request.client.host).is_loopback:
            return
    except ValueError:
        pass
    raise HTTPException(status_code=403, detail="Evaluation is available to the local operator.")


def _read(run_id):
    try:
        return evaluation_service.read_evaluation(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Evaluation not found.") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Evaluation evidence is unavailable.") from exc


@router.post("", response_model=EvaluationReport, status_code=202)
def create_evaluation(payload: EvaluationRequest, request: Request, background: BackgroundTasks, response: Response):
    _require_local(request)
    turns = payload.repeats * sum(agent.max_turns for agent in payload.agents)
    if turns and not reserve_model("local", turns):
        raise HTTPException(429, "Daily model request budget reached.")
    try:
        report = evaluation_service.create_evaluation(payload)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Evaluation storage is unavailable.") from exc
    background.add_task(evaluation_service.run_evaluation, report.id, payload)
    response.headers["Location"] = f"/evaluations/{report.id}"
    return report


@router.get("/{run_id}", response_model=EvaluationReport)
def get_evaluation(run_id: str, request: Request):
    _require_local(request)
    return _read(run_id)


@router.get("/{run_id}/summary.csv")
def get_evaluation_csv(run_id: str, request: Request):
    _require_local(request)
    report = _read(run_id)
    return Response(summary_csv(report), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="evaluation-{report.id}.csv"'})


@router.get("/{run_id}/trials.csv")
def get_trials_csv(run_id: str, request: Request):
    _require_local(request)
    report = _read(run_id)
    return Response(trials_csv(report), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="trials-{report.id}.csv"'})
