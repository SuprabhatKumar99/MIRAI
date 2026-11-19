from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, Field, HttpUrl

SnapshotId = Annotated[str, Field(pattern=r"^snap_[0-9a-f]{64}$")]
ContentSHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BaselineTestCommand(BaseModel):
    framework: str
    working_directory: str
    argv: list[str]


class RepoAnalysisRequest(BaseModel):
    repo_url: HttpUrl = Field(
        ...,
        description="Public GitHub repository URL (e.g., https://github.com/owner/repo)",
        examples=["https://github.com/fastapi/fastapi"],
    )
    commit_sha: str | None = Field(
        default=None, pattern=r"^[0-9a-fA-F]{40}$",
        description="Optional full GitHub commit SHA. Defaults to the current default-branch commit.",
    )


class RepoAnalysisResponse(BaseModel):
    project_name: str = Field(..., description="Name of the project or repository")
    source_type: Literal["github", "zip"] = Field(
        ..., description="Source of the codebase ('github' or 'zip')"
    )
    detected_languages: list[str] = Field(
        default_factory=list, description="Primary programming languages detected"
    )
    detected_frameworks: list[str] = Field(
        default_factory=list, description="Frameworks or libraries detected (e.g., FastAPI, React)"
    )
    file_count: int = Field(default=0, ge=0, description="Total count of analyzed files")
    folder_structure: list[str] = Field(
        default_factory=list, description="High-level project folder hierarchy"
    )
    readme_summary: str | None = Field(
        default=None, description="Extracted or truncated content from README file if present"
    )
    analysis_status: Literal["pending", "completed", "failed"] = Field(
        default="completed", description="Status of the repository analysis"
    )
    commit_sha: str | None = Field(
        default=None,
        description="Git commit SHA of the analyzed repository state when available",
        examples=["7fac3b2c12a84e3a4e402b8d9620b7c7b89d412e"],
    )
    snapshot_id: SnapshotId | None = None
    content_sha256: ContentSHA256 | None = None
    repository_identity: str | None = None
    retrieved_at: datetime | None = None
    test_framework: str | None = None
    test_frameworks: list[str] = Field(default_factory=list)
    test_framework_config_file: str | None = None
    existing_test_files: list[str] = Field(default_factory=list)
    baseline_test_commands: list[BaselineTestCommand] = Field(default_factory=list)


class SnapshotRecord(BaseModel):
    schema_version: Literal[1] = 1
    analysis: RepoAnalysisResponse
    archive_location: str
