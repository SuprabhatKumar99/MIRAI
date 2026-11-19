"""Local Harbor evaluation requests and auditable trial results."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.harbor import HarborExportRequest
from app.schemas.repo import ContentSHA256, SnapshotId

RUN_ID = r"^[0-9a-f]{32}$"
AGENT_NAME = "ctf-file-agent"
AGENT_VERSION = "1.0.0"
Failure = Literal[
    "provider_unavailable", "environment_failure", "timeout", "agent_crash", "agent_incomplete",
    "build_failed", "feature_failed", "regression_failed", "regression_protection", "task_defect",
    "invalid_evidence", "task_admission", "busy",
]


class AgentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
    model: str = Field(pattern=r"^(?:(?:openai|gemini)/[A-Za-z0-9][A-Za-z0-9._-]{0,89}|huggingface/[A-Za-z0-9][A-Za-z0-9._-]{0,79}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?::[A-Za-z0-9_-]{1,40})?)$")
    fallback_model: str | None = Field(
        default=None,
        pattern=r"^(?:gemini/[A-Za-z0-9][A-Za-z0-9._-]{0,89}|huggingface/[A-Za-z0-9][A-Za-z0-9._-]{0,79}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?::[A-Za-z0-9_-]{1,40})?)$",
    )
    max_turns: int = Field(default=5, ge=1, le=8)
    max_output_tokens: int = Field(default=1024, ge=256, le=2048)
    temperature: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def fallback_is_for_openai(self):
        if self.fallback_model and (
            self.model.startswith("gemini/") or self.fallback_model == self.model
            or (self.model.startswith("huggingface/") and not self.fallback_model.startswith("gemini/"))
        ):
            raise ValueError("A fallback applies to an OpenAI or Hugging Face primary configuration.")
        return self


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: HarborExportRequest
    mode: Literal["agent", "control"] = "agent"
    agents: list[AgentConfiguration] = Field(default_factory=list, max_length=3)
    repeats: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def bound_campaign(self):
        if bool(self.agents) != (self.mode == "agent"):
            raise ValueError("Agent runs need a model configuration. Control runs take no agent configuration.")
        if len({agent.label.casefold() for agent in self.agents}) != len(self.agents):
            raise ValueError("Comparison labels must be unique.")
        if self.repeats * max(1, len(self.agents)) > 6:
            raise ValueError("A campaign supports up to six scored or control trials.")
        return self


class PackageEvidence(BaseModel):
    task_id: str
    language: str
    category: str
    difficulty: str
    snapshot_id: SnapshotId
    snapshot_sha256: ContentSHA256
    starting_commit_sha: str | None = None
    task_sha256: ContentSHA256
    solution_sha256: ContentSHA256
    test_suite_sha256: ContentSHA256
    dependency_lock_sha256: ContentSHA256
    package_sha256: ContentSHA256
    package_tree_sha256: ContentSHA256
    verifier_plan_sha256: ContentSHA256
    runtime_image: str
    validation_id: str | None = None
    agent_timeout_seconds: int = 120
    verifier_timeout_seconds: int = 180
    memory_mb: int = 512
    cpus: int = 1
    pids_limit: int = 64


class ProcessEvidence(BaseModel):
    argv: list[str]
    exit_code: int | None = None
    console_log: str
    started_at: datetime
    finished_at: datetime
    timed_out: bool = False
    output_truncated: bool = False


class TestCounts(BaseModel):
    passed: int = Field(ge=0)
    total: int = Field(ge=1)


class ChangedFile(BaseModel):
    path: str
    operation: Literal["create", "modify", "delete"]
    before_sha256: ContentSHA256 | None = None
    after_sha256: ContentSHA256 | None = None


class TrialEvidence(BaseModel):
    requested_model: str | None = None
    role: Literal["oracle", "agent", "control"]
    origin: Literal["harbor", "test_double"]
    label: str
    attempt: int = Field(ge=1)
    configuration: AgentConfiguration | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    resolved_models: list[str] = Field(default_factory=list)
    provider_calls: int | None = None
    harbor_trial_id: str | None = None
    harbor_task_checksum: str | None = None
    process: ProcessEvidence | None = None
    reward: Literal[0, 1] | None = None
    trustworthy: bool = False
    failure: Failure | None = None
    exception_type: str | None = None
    build_passed: bool | None = None
    feature_tests: TestCounts | None = None
    feature_failure_kind: str | None = None
    regression_tests: TestCounts | None = None
    agent_duration_seconds: float | None = None
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    submitted_files: list[ChangedFile] | None = None
    evidence: dict[str, str] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)

    @property
    def scored(self) -> bool:
        return self.role == "agent" and self.origin == "harbor" and self.trustworthy and self.reward is not None


class Rate(BaseModel):
    numerator: int
    denominator: int
    value: float | None = None
    wilson95: tuple[float, float] | None = None


class Metrics(BaseModel):
    label: str
    scheduled: int
    recorded: int
    scored: int
    verified_success: Rate
    feature_pass: Rate
    regression_pass: Rate
    build_success: Rate
    failure_counts: dict[str, int]
    mean_agent_seconds: float | None = None
    agent_seconds_observed: int = 0
    sample_agent_seconds_sd: float | None = None
    input_tokens_total: int | None = None
    input_tokens_observed: int = 0
    output_tokens_total: int | None = None
    output_tokens_observed: int = 0
    cost_usd_total: float | None = None
    cost_observed: int = 0


class EvaluationReport(BaseModel):
    id: str = Field(pattern=RUN_ID)
    state: Literal["queued", "running", "completed", "blocked", "quarantined", "failed"] = "queued"
    mode: Literal["agent", "control"]
    agents: list[AgentConfiguration]
    repeats: int
    task_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    package: PackageEvidence | None = None
    harbor_version: str | None = None
    docker_engine: str | None = None
    agent_code_sha256: ContentSHA256 | None = None
    oracle: TrialEvidence | None = None
    baseline_control: TrialEvidence | None = None
    trials: list[TrialEvidence] = Field(default_factory=list)
    failure: Failure | None = None
    detail: str | None = None
    metrics: list[Metrics] = Field(default_factory=list)
    strata: dict[str, dict[str, Metrics]] = Field(default_factory=dict)
