"""Catalogue responses and managed operation requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.evaluation import AgentConfiguration
from app.schemas.repo import RepoAnalysisResponse
from app.schemas.solution import SolutionResponse
from app.schemas.task import TaskResponse
from app.schemas.test_suite import TestSuiteResponse
from app.schemas.validation import ValidationReport

Kind = Literal["solution", "tests", "validation", "export", "evaluation"]
JobState = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted", "superseded"]


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    revision: int = Field(ge=0)
    request_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{12,80}$")
    task_prefix: str = Field(default="ctf", pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    mode: Literal["agent", "control"] = "control"
    agents: list[AgentConfiguration] = Field(default_factory=list, max_length=3)
    repeats: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def validate_campaign(self):
        if self.kind != "evaluation":
            if self.agents or self.mode != "control" or self.repeats != 1:
                raise ValueError("Agent settings apply to evaluation jobs.")
        elif (bool(self.agents) != (self.mode == "agent")
              or len({value.label.casefold() for value in self.agents}) != len(self.agents)
              or max(1, len(self.agents)) * self.repeats > 6):
            raise ValueError("Use distinct model configurations and no more than six trials.")
        return self


class JobSummary(BaseModel):
    id: str
    kind: Kind
    state: JobState
    cancel_requested: bool
    result_id: str | None
    error: str | None


class JobResponse(JobSummary):
    task_id: str
    revision: int
    created_at: str
    updated_at: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    updated_at: str
    snapshot: RepoAnalysisResponse


class ProjectList(BaseModel):
    projects: list[ProjectSummary]
    limit: int
    offset: int


class TaskState(BaseModel):
    revision: int
    project_id: str | None
    updated_at: str
    verdict: Literal["VALIDATED", "REJECTED", "NOT_VALIDATED"] | None


class TaskList(BaseModel):
    tasks: list[TaskResponse]
    total: int
    limit: int
    offset: int
    states: list[TaskState]


class TaskDetail(BaseModel):
    task: TaskResponse
    project_id: str | None
    revision: int
    snapshot: RepoAnalysisResponse | None
    solution: SolutionResponse | None
    test_suite: TestSuiteResponse | None
    validation: ValidationReport | None
    export: dict | None
    jobs: list[JobSummary]


class HistoryItem(BaseModel):
    id: str
    kind: Kind
    created_at: str
    solution_id: str | None
    suite_id: str | None
    status: str | None


class HistoryPage(BaseModel):
    items: list[HistoryItem]
    limit: int
    offset: int
