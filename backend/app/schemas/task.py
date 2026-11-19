from typing import Literal
from pydantic import BaseModel, Field, HttpUrl, model_validator
from app.schemas.repo import RepoAnalysisResponse, SnapshotId


class TaskRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    difficulty: str = Field(default="medium", max_length=32)
    language: str = Field(default="python", max_length=40)
    category: str = Field(default="backend", max_length=60)


class RepoTaskRequest(BaseModel):
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    focus_area: str | None = Field(
        default=None,
        max_length=500,
        description="Optional focus area or feature to work on (e.g. 'authentication', 'caching')",
        examples=["Add rate limiting middleware"],
    )
    category: str | None = Field(
        default=None,
        max_length=60,
        description="Optional category override (e.g. backend, frontend, devops)",
        examples=["backend"],
    )


class GenerateFromRepoRequest(BaseModel):
    repo_analysis: RepoAnalysisResponse | None = None
    snapshot_id: SnapshotId | None = None
    options: RepoTaskRequest = Field(default_factory=RepoTaskRequest)

    @model_validator(mode="after")
    def require_repository_reference(self) -> "GenerateFromRepoRequest":
        if self.repo_analysis is None and self.snapshot_id is None:
            raise ValueError("Provide a repository snapshot ID or repository analysis.")
        return self


class GenerateFromGithubRequest(BaseModel):
    repo_url: HttpUrl = Field(
        ...,
        description="Public GitHub repository URL (e.g., https://github.com/owner/repo)",
        examples=["https://github.com/fastapi/fastapi"],
    )
    options: RepoTaskRequest = Field(default_factory=RepoTaskRequest)


class TaskResponse(BaseModel):
    id: str
    topic: str
    difficulty: str
    language: str
    category: str
    title: str
    description: str
    requirements: list[str]
    acceptance_criteria: list[str]
    generated_at: str                 # ISO-8601 UTC timestamp
    project_name: str | None = None
    relevant_files: list[str] = Field(default_factory=list)
    snapshot_id: SnapshotId | None = None
    starting_commit_sha: str | None = Field(
        default=None,
        description="Starting Git commit SHA when available",
        examples=["7fac3b2c12a84e3a4e402b8d9620b7c7b89d412e"],
    )
