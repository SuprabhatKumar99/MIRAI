from typing import Literal
from pydantic import BaseModel, Field, model_validator

from app.schemas.repo import ContentSHA256, SnapshotId
from app.schemas.task import TaskResponse

FileOperation = Literal["create", "modify", "delete"]


class SolutionFile(BaseModel):
    path: str = Field(
        ...,
        description="Relative file path within the project (e.g. 'app/services/cache.py')",
        examples=["app/services/cache.py"],
    )
    status: FileOperation = Field(
        default="create",
        description="Operation to apply to a repository-relative file",
    )
    language: str = Field(
        default="python",
        description="Programming language of the code in this file",
        examples=["python", "typescript"],
    )
    code: str = Field(
        ...,
        description="Complete UTF-8 replacement content, or an empty string for deletion",
    )
    explanation: str = Field(
        ...,
        description="Explanation of the changes, design decisions, and how they fulfill requirements",
    )
    requirement_ids: list[str] = Field(default_factory=list)
    acceptance_criteria_ids: list[str] = Field(default_factory=list)
    base_sha256: ContentSHA256 | None = None
    result_sha256: ContentSHA256 | None = None

    @model_validator(mode="after")
    def validate_delete_content(self) -> "SolutionFile":
        if self.status == "delete" and (self.code or self.result_sha256):
            raise ValueError("Deletion must have empty content and no result checksum.")
        return self


class SolutionContextFile(BaseModel):
    path: str
    kind: Literal["source", "test", "config", "readme"]
    complete: bool


class SolutionStructuralCheck(BaseModel):
    status: Literal["applied_to_snapshot", "not_checked"]
    execution_status: Literal["not_run"] = "not_run"
    model_attempts: int
    input_bytes: int
    estimated_input_tokens: int


class SolutionCriterion(BaseModel):
    id: str
    text: str


class SolutionResponse(BaseModel):
    id: str = Field(..., description="Unique UUID identifier for this solution")
    task_id: str = Field(..., description="UUID of the task this solution solves")
    task_title: str = Field(..., description="Title of the task")
    snapshot_id: SnapshotId | None = None
    context_files: list[SolutionContextFile] = Field(default_factory=list)
    structural_check: SolutionStructuralCheck | None = None
    requirements: list[SolutionCriterion] = Field(default_factory=list)
    acceptance_criteria: list[SolutionCriterion] = Field(default_factory=list)
    architecture_notes: str = Field(
        ...,
        description="High-level architectural overview, component interactions, and design trade-offs",
    )
    files: list[SolutionFile] = Field(
        default_factory=list,
        description="File operations implementing the solution",
    )
    testing_guide: str = Field(
        ...,
        description="Guidance and instructions for writing tests to verify the solution",
    )
    verification_steps: list[str] = Field(
        default_factory=list,
        description="Concrete, actionable steps to manually or automatically verify the solution satisfies acceptance criteria",
    )
    generated_at: str = Field(
        ...,
        description="ISO-8601 UTC timestamp when the solution was generated",
    )


class SolutionRequest(BaseModel):
    task: TaskResponse = Field(
        ...,
        description="The generated coding task to produce a reference solution for",
    )
    authorized_deletions: list[str] = Field(
        default_factory=list, max_length=12, description="Exact existing paths approved for deletion."
    )
