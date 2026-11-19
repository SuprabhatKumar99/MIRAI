import asyncio
import sqlite3
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.core.config import APP_NAME, GEMINI_API_KEY
from app.core.access import AccessMiddleware, origins, shared
from app.core.gemini_errors import logger, model_label
from app.routers import evaluations, health, tasks, workflow
from app.services import workflow_jobs
from app.services.workflow_store import WorkflowConflict


@asynccontextmanager
async def lifespan(_app):
    logger.info("Gemini model: %s. API key configured: %s.", model_label(), bool(GEMINI_API_KEY))

    async def recover_queue():
        while True:
            with suppress(OSError, RuntimeError, sqlite3.Error):
                await asyncio.to_thread(workflow_jobs.drain_jobs)
            await asyncio.sleep(2)

    worker = asyncio.create_task(recover_queue())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(title=APP_NAME, lifespan=lifespan)
allowed_origins = origins()
app.add_middleware(AccessMiddleware, allowed_origins=allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Location", "Content-Disposition", "X-Harbor-Task-Name", "X-Validation-Status"],
)


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if shared():
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, exc):
    return JSONResponse({"detail": [{"loc": error["loc"], "msg": ("Artifact task ID does not match Task ID." if "does not match Task ID" in error["msg"] else "Invalid value."), "type": error["type"]}
                                   for error in exc.errors()]}, status_code=422)


@app.exception_handler(WorkflowConflict)
async def workflow_conflict(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


app.include_router(health.router)
app.include_router(tasks.router)
app.include_router(evaluations.router)
app.include_router(workflow.router)

