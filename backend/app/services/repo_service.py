"""
Repository Scanner & Ingestion Service.

Provides:
- SSRF-protected GitHub repository archive downloading.
- Hardened ZIP archive validation & extraction.
- Recursive, safe static scanning of codebases (no code execution).
"""

import ipaddress
import json
import os
import re
import socket
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import quote, urljoin, urlsplit

import requests

from app.schemas.repo import BaselineTestCommand, RepoAnalysisResponse
from app.services import snapshot_service
from app.services.archive_service import CHUNK_SIZE, MAX_ZIP_SIZE_BYTES, safe_extract_zip

# ── Security & Limits Constants ─────────────────────────────────────

NETWORK_CONNECT_TIMEOUT_SEC: float = 5.0
NETWORK_READ_TIMEOUT_SEC: float = 30.0
MAX_REDIRECT_HOPS: int = 3

ALLOWED_GITHUB_HOSTS: set[str] = {"github.com", "api.github.com", "codeload.github.com"}

IGNORED_DIRS: set[str] = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".idea",
    ".vscode",
    ".next",
    ".nuxt",
    "target",
    "bin",
    "obj",
}

IGNORED_EXTENSIONS: set[str] = {
    # Binaries & executables
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".pyc", ".pyo", ".class",
    # Archives & bundles
    ".zip", ".tar", ".gz", ".7z", ".rar", ".jar", ".war",
    # Media
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".mp3", ".mp4", ".wav", ".avi",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Documents
    ".pdf", ".docx", ".xlsx", ".pptx",
    # DB & data dumps
    ".db", ".sqlite", ".sqlite3", ".parquet",
}

MAX_FILES_TO_SCAN: int = 1000
MAX_FILE_SIZE_BYTES: int = 500 * 1024  # 500 KB limit per file inspected
MAX_README_CHARS: int = 2000

# ── Extension to Language Mapping ───────────────────────────────────

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript (React)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (React)",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".h": "C/C++ Header",
    ".hpp": "C++ Header",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
}

PYTHON_FRAMEWORK_SIGNATURES: dict[str, str] = {
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "pydantic": "Pydantic",
    "sqlalchemy": "SQLAlchemy",
    "tortoise-orm": "Tortoise ORM",
    "celery": "Celery",
    "pytest": "Pytest",
    "pandas": "Pandas",
    "numpy": "NumPy",
    "tensorflow": "TensorFlow",
    "torch": "PyTorch",
    "google-genai": "Google GenAI",
    "openai": "OpenAI SDK",
}

JS_FRAMEWORK_SIGNATURES: dict[str, str] = {
    "react": "React",
    "next": "Next.js",
    "vue": "Vue.js",
    "nuxt": "Nuxt.js",
    "@angular/core": "Angular",
    "svelte": "Svelte",
    "express": "Express",
    "@nestjs/core": "NestJS",
    "tailwindcss": "Tailwind CSS",
    "prisma": "Prisma",
    "mongoose": "Mongoose",
}

JAVA_FRAMEWORK_SIGNATURES: dict[str, str] = {
    "spring-boot": "Spring Boot",
    "spring-web": "Spring MVC",
    "hibernate": "Hibernate",
    "quarkus": "Quarkus",
}

GO_FRAMEWORK_SIGNATURES: dict[str, str] = {
    "github.com/gin-gonic/gin": "Gin",
    "github.com/labstack/echo": "Echo",
    "github.com/gofiber/fiber": "Fiber",
    "gorm.io/gorm": "GORM",
}


# ── SSRF & Network Security ─────────────────────────────────────────

class GitHubRateLimitError(RuntimeError):
    """GitHub is throttling unauthenticated public repository requests."""


class _PinnedHTTPSAdapter(requests.adapters.HTTPAdapter):
    """Connect to the verified IP while preserving HTTPS SNI and certificate checks."""

    def __init__(self, hostname: str, address: str):
        self.hostname = hostname
        self.address = address
        super().__init__()

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = self.address
        pool_kwargs.update(assert_hostname=self.hostname, server_hostname=self.hostname)
        return host_params, pool_kwargs


def _is_ip_safe(ip_str: str) -> bool:
    try:
        address = ipaddress.ip_address(ip_str)
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


def _verify_hostname_and_dns(hostname: str) -> str:
    if hostname not in ALLOWED_GITHUB_HOSTS:
        raise ValueError("URL is not on a trusted GitHub domain.")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RuntimeError(f"DNS resolution failed for '{hostname}'.") from exc
    if not addresses:
        raise RuntimeError(f"DNS resolution returned no addresses for '{hostname}'.")
    if any(not _is_ip_safe(entry[4][0]) for entry in addresses):
        raise ValueError(f"Host '{hostname}' resolved to a disallowed/private IP.")
    # Prefer IPv4 when available on dual-stack Windows and Docker hosts.
    return min(addresses, key=lambda entry: entry[0] != socket.AF_INET)[4][0]


def parse_and_validate_github_url(repo_url: str) -> tuple[str, str]:
    """Accept only public GitHub repository URLs of the form /owner/repo."""
    parts = urlsplit(repo_url)
    if parts.scheme != "https":
        raise ValueError("Only HTTPS repository URLs are allowed.")
    if parts.hostname != "github.com":
        raise ValueError("Invalid host. Expected 'github.com'.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are not permitted.")
    if parts.port not in (None, 443):
        raise ValueError("Only default HTTPS port (443) is permitted.")
    if parts.query or parts.fragment:
        raise ValueError("Repository URL must not include a query or fragment.")
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", parts.path)
    if not match or any(value in (".", "..") for value in match.groups()):
        raise ValueError("Invalid GitHub repository path. Expected '/owner/repo'.")
    return match.group(1), match.group(2)


@contextmanager
def _github_response(url: str, repository: str, archive_sha: str | None = None):
    for _ in range(MAX_REDIRECT_HOPS + 1):
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host not in (ALLOWED_GITHUB_HOSTS if archive_sha else {"api.github.com"}):
            raise ValueError("URL is not on a trusted GitHub domain.")
        if (
            parts.scheme != "https"
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Unsafe GitHub request or redirect URL.")
        if archive_sha:
            owner_repo = repository.lower()
            allowed_paths = {
                "api.github.com": {f"/repos/{owner_repo}/zipball/{archive_sha}"},
                "github.com": {f"/{owner_repo}/archive/{archive_sha}.zip"},
                "codeload.github.com": {
                    f"/{owner_repo}/legacy.zip/{archive_sha}",
                    f"/{owner_repo}/zip/{archive_sha}",
                },
            }
            if parts.path.lower() not in allowed_paths[host]:
                raise ValueError("GitHub archive redirect does not preserve the resolved commit.")
        address = _verify_hostname_and_dns(host)
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.mount("https://", _PinnedHTTPSAdapter(host, address))
                with session.get(
                    url,
                    stream=True,
                    allow_redirects=False,
                    timeout=(NETWORK_CONNECT_TIMEOUT_SEC, NETWORK_READ_TIMEOUT_SEC),
                    headers={
                        "Host": host,
                        "User-Agent": "CodingTaskFactory-Bot/1.0",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                ) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("GitHub redirect is missing a Location header.")
                        url = urljoin(url, location)
                        continue
                    if status == 404:
                        if archive_sha:
                            raise FileNotFoundError("The resolved GitHub commit archive is unavailable.")
                        raise FileNotFoundError(f"GitHub repository '{repository}' was not found or is private.")
                    if status == 429 or (
                        status == 403
                        and (response.headers.get("X-RateLimit-Remaining") == "0"
                             or response.headers.get("Retry-After"))
                    ):
                        raise GitHubRateLimitError("GitHub rate limit reached. Retry later.")
                    if status in (409, 422) and not archive_sha:
                        raise ValueError("GitHub repository does not have a resolvable default-branch commit.")
                    if status != 200:
                        raise RuntimeError(f"GitHub returned HTTP status {status}.")
                    yield response
                    return
        except requests.RequestException as exc:
            raise RuntimeError("Network error contacting GitHub.") from exc
    raise ValueError("Exceeded maximum allowed GitHub redirect hops.")


def _github_json(url: str, repository: str) -> dict:
    response_bytes = bytearray()
    with _github_response(url, repository) as response:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            response_bytes.extend(chunk)
            if len(response_bytes) > 1024 * 1024:
                raise RuntimeError("GitHub metadata response exceeds the size limit.")
    try:
        payload = json.loads(response_bytes)
    except ValueError as exc:
        raise RuntimeError("GitHub returned invalid JSON metadata.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned invalid repository metadata.")
    return payload


def resolve_github_revision(owner: str, repo: str, revision: str | None = None) -> tuple[str, str, str]:
    """Verify a requested commit or resolve the repository's default branch."""
    if revision is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("A full GitHub commit SHA is required.")
    repository = f"{owner}/{repo}"
    metadata = _github_json(f"https://api.github.com/repos/{repository}", repository)
    if metadata.get("private") is True:
        raise FileNotFoundError(f"GitHub repository '{repository}' was not found or is private.")
    full_name = metadata.get("full_name")
    if not isinstance(full_name, str):
        raise RuntimeError("GitHub did not return a repository identity.")
    try:
        canonical_owner, canonical_repo = parse_and_validate_github_url(f"https://github.com/{full_name}")
    except ValueError as exc:
        raise RuntimeError("GitHub returned an invalid repository identity.") from exc

    repository = f"{canonical_owner}/{canonical_repo}"
    if revision is not None:
        revision = revision.lower()
        commit = _github_json(f"https://api.github.com/repos/{repository}/commits/{revision}", repository)
        sha = commit.get("sha")
        if not isinstance(sha, str) or sha.lower() != revision:
            raise RuntimeError("GitHub did not confirm the requested full commit SHA.")
        return canonical_owner, canonical_repo, revision

    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch or any(
        part in ("", ".", "..") for part in branch.split("/")
    ):
        raise RuntimeError("GitHub did not return a valid default branch.")
    try:
        branch_info = _github_json(
            f"https://api.github.com/repos/{repository}/branches/{quote(branch, safe='')}", repository
        )
    except FileNotFoundError as exc:
        raise ValueError("GitHub repository's default branch has no available commit.") from exc
    commit = branch_info.get("commit")
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if branch_info.get("name") != branch or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise RuntimeError("GitHub did not return a full commit SHA for its default branch.")
    return canonical_owner, canonical_repo, sha.lower()


def download_github_archive(owner: str, repo: str, commit_sha: str, destination_file: Path) -> None:
    """Download a ZIP via GitHub's immutable ref endpoint and validate the redirect chain."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ValueError("A full GitHub commit SHA is required for archive download.")
    repository = f"{owner}/{repo}"
    url = f"https://api.github.com/repos/{repository}/zipball/{commit_sha}"
    with _github_response(url, repository, commit_sha) as response:
        with destination_file.open("xb") as target:
            size = 0
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_ZIP_SIZE_BYTES:
                    raise ValueError("Repository archive exceeds the maximum download size.")
                target.write(chunk)

    try:
        with zipfile.ZipFile(destination_file) as archive:
            comment = archive.comment
    except zipfile.BadZipFile as exc:
        raise RuntimeError("GitHub returned a corrupt ZIP archive.") from exc
    if re.fullmatch(rb"[0-9a-fA-F]{40}", comment) and comment.decode("ascii").lower() != commit_sha:
        raise RuntimeError("GitHub archive commit metadata does not match the resolved SHA.")


def stream_upload_to_disk(upload_stream: BinaryIO, destination: Path) -> int:
    """Stream incoming upload file in chunks to disk, checking size limit on the fly."""
    total_bytes = 0
    with open(destination, "wb") as out_f:
        while True:
            chunk = upload_stream.read(CHUNK_SIZE)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_ZIP_SIZE_BYTES:
                raise ValueError(
                    f"Uploaded file exceeds maximum limit of {MAX_ZIP_SIZE_BYTES // (1024 * 1024)} MB."
                )
            out_f.write(chunk)
    return total_bytes


# ── Repository Analysis Functions ───────────────────────────────────

def analyze_github_repository(repo_url: str, *, revision: str | None = None) -> RepoAnalysisResponse:
    """Download, safely unpack, and analyze a public GitHub repository."""
    owner, repo = parse_and_validate_github_url(repo_url)
    owner, repo, commit_sha = resolve_github_revision(owner, repo, revision)

    with tempfile.TemporaryDirectory(prefix="ctf_github_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        zip_file = temp_dir / "repo.zip"
        extract_dir = temp_dir / "extracted"
        extract_dir.mkdir()

        # Step 1: Download
        download_github_archive(owner, repo, commit_sha, zip_file)

        # Step 2: Extract safely
        source_root = safe_extract_zip(zip_file, extract_dir)

        # Step 3: Scan codebase
        analysis = scan_local_repository(
            repo_path=source_root,
            project_name=repo,
            source_type="github",
        )
        return snapshot_service.publish_snapshot(
            zip_file,
            analysis,
            github_identity=f"github.com/{owner.lower()}/{repo.lower()}",
            commit_sha=commit_sha,
        )


def analyze_uploaded_zip(file_stream: BinaryIO, filename: str) -> RepoAnalysisResponse:
    """Stream, safely unpack, and analyze an uploaded ZIP file."""
    base_name = Path(filename.replace("\\", "/")).stem or "uploaded_project"

    with tempfile.TemporaryDirectory(prefix="ctf_upload_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        zip_file = temp_dir / "upload.zip"
        extract_dir = temp_dir / "extracted"
        extract_dir.mkdir()

        # Step 1: Stream to disk
        stream_upload_to_disk(file_stream, zip_file)

        # Step 2: Extract safely
        source_root = safe_extract_zip(zip_file, extract_dir)

        # Step 3: Scan codebase
        analysis = scan_local_repository(
            repo_path=source_root,
            project_name=base_name,
            source_type="zip",
        )
        return snapshot_service.publish_snapshot(zip_file, analysis)


# ── Local Repository Scanner ────────────────────────────────────────

def _safe_read_text(file_path: Path, max_chars: int | None = None) -> str:
    """Read text from file safely with size and encoding fallback."""
    try:
        if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            return ""
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        if max_chars and len(content) > max_chars:
            return content[:max_chars].strip() + "\n...(truncated)"
        return content
    except Exception:
        return ""


def _extract_frameworks_from_python_config(content: str) -> set[str]:
    detected = set()
    lower = content.lower()
    for signature, framework_name in PYTHON_FRAMEWORK_SIGNATURES.items():
        if signature in lower:
            detected.add(framework_name)
    return detected


def _extract_frameworks_from_package_json(content: str) -> set[str]:
    detected = set()
    try:
        data = json.loads(content)
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        deps_keys = {k.lower() for k in deps.keys()}
        for signature, framework_name in JS_FRAMEWORK_SIGNATURES.items():
            if signature.lower() in deps_keys:
                detected.add(framework_name)
    except Exception:
        pass
    return detected


def _extract_frameworks_from_pom_xml(content: str) -> set[str]:
    detected = set()
    lower = content.lower()
    for signature, framework_name in JAVA_FRAMEWORK_SIGNATURES.items():
        if signature in lower:
            detected.add(framework_name)
    return detected


def _extract_frameworks_from_go_mod(content: str) -> set[str]:
    detected = set()
    for signature, framework_name in GO_FRAMEWORK_SIGNATURES.items():
        if signature in content:
            detected.add(framework_name)
    return detected


def scan_local_repository(
    repo_path: str | Path,
    project_name: str | None = None,
    source_type: Literal["github", "zip"] = "zip",
) -> RepoAnalysisResponse:
    """Recursively inspects a local repository path and returns a structured RepoAnalysisResponse."""
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Repository path does not exist or is not a directory: {repo_path}")

    actual_project_name = project_name or root.name

    language_counts: dict[str, int] = {}
    detected_frameworks: set[str] = set()
    folders: set[str] = set()
    readme_content: str | None = None
    scanned_file_count = 0

    for item in root.rglob("*"):
        if scanned_file_count >= MAX_FILES_TO_SCAN:
            break

        try:
            rel_parts = item.relative_to(root).parts
        except ValueError:
            continue

        if any(part in IGNORED_DIRS for part in rel_parts):
            continue

        if item.is_dir():
            if len(rel_parts) <= 2:
                folders.add("/".join(rel_parts))
            continue

        if not item.is_file():
            continue

        suffix = item.suffix.lower()
        if suffix in IGNORED_EXTENSIONS:
            continue

        scanned_file_count += 1
        name_lower = item.name.lower()

        if name_lower in ("readme.md", "readme.txt", "readme"):
            if readme_content is None:
                readme_content = _safe_read_text(item, max_chars=MAX_README_CHARS)

        if suffix in EXTENSION_TO_LANGUAGE:
            lang = EXTENSION_TO_LANGUAGE[suffix]
            language_counts[lang] = language_counts.get(lang, 0) + 1

        if name_lower in ("requirements.txt", "pyproject.toml", "pipfile"):
            content = _safe_read_text(item)
            detected_frameworks.update(_extract_frameworks_from_python_config(content))

        elif name_lower == "package.json":
            content = _safe_read_text(item)
            detected_frameworks.update(_extract_frameworks_from_package_json(content))

        elif name_lower == "pom.xml":
            content = _safe_read_text(item)
            detected_frameworks.update(_extract_frameworks_from_pom_xml(content))

        elif name_lower == "go.mod":
            content = _safe_read_text(item)
            detected_frameworks.update(_extract_frameworks_from_go_mod(content))

    sorted_languages = [
        lang for lang, _ in sorted(language_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    sorted_frameworks = sorted(detected_frameworks)
    sorted_folders = sorted(folders)

    return RepoAnalysisResponse(
        project_name=actual_project_name,
        source_type=source_type,
        detected_languages=sorted_languages,
        detected_frameworks=sorted_frameworks,
        file_count=scanned_file_count,
        folder_structure=sorted_folders,
        readme_summary=readme_content,
        analysis_status="completed",
        **detect_test_framework_and_files(root),
    )


# ── Test Framework & Existing Test Detection ───────────────────────

TEST_CONFIG_FILENAMES: dict[str, str] = {
    "pytest.ini": "pytest",
    ".pytest.ini": "pytest",
    "jest.config.js": "jest",
    "jest.config.ts": "jest",
    "jest.config.mjs": "jest",
    "jest.config.json": "jest",
    "vitest.config.ts": "vitest",
    "vitest.config.js": "vitest",
    "vitest.config.mjs": "vitest",
    ".mocharc.json": "mocha",
    ".mocharc.js": "mocha",
    ".mocharc.yml": "mocha",
    ".mocharc.yaml": "mocha",
}


def _is_test_file(rel_path: str, filename_lower: str, suffix: str) -> bool:
    """Check whether a file matches conventional test naming conventions."""
    parts_lower = [p.lower() for p in rel_path.split("/")]

    # Check directory name hints
    in_test_dir = any(d in parts_lower[:-1] for d in ("tests", "test", "__tests__", "spec", "specs"))

    if suffix == ".py":
        return filename_lower.startswith("test_") or filename_lower.endswith("_test.py") or (in_test_dir and suffix == ".py")

    if suffix in (".js", ".jsx", ".ts", ".tsx"):
        return (
            ".test." in filename_lower
            or ".spec." in filename_lower
            or (in_test_dir and suffix in (".js", ".jsx", ".ts", ".tsx"))
        )

    if suffix == ".go":
        return filename_lower.endswith("_test.go")

    return False


def _baseline_test_commands(
    root: Path,
    configs: list[tuple[str, str]],
    test_files: set[str],
    imported_frameworks: list[tuple[str, str]],
) -> list[BaselineTestCommand]:
    commands: set[tuple[str, str, tuple[str, ...]]] = set()
    for framework, config in configs:
        scope = Path(config).parent
        scoped_files = [Path(file) for file in test_files if Path(file).is_relative_to(scope)]
        if framework == "pytest" and any(file.suffix == ".py" for file in scoped_files):
            commands.add((framework, scope.as_posix(), ("python", "-m", "pytest")))
        if Path(config).name != "package.json" or not any(
            file.suffix in (".js", ".jsx", ".ts", ".tsx") for file in scoped_files
        ):
            continue
        package = json.loads(_safe_read_text(root / config))
        scripts = package.get("scripts", {})
        script = scripts.get("test") if isinstance(scripts, dict) else None
        if not isinstance(script, str) or not re.match(rf"^\s*{framework}(?:\s|$)", script):
            continue
        manager = str(package.get("packageManager", "")).partition("@")[0]
        if not manager:
            if (root / scope / "pnpm-lock.yaml").is_file():
                manager = "pnpm"
            elif (root / scope / "yarn.lock").is_file():
                manager = "yarn"
            elif (root / scope / "package-lock.json").is_file():
                manager = "npm"
        if manager in ("npm", "pnpm", "yarn"):
            commands.add((framework, scope.as_posix(), (manager, "test")))

    for framework, filename in imported_frameworks:
        path = Path(filename)
        if framework == "unittest" and path.name.startswith("test"):
            # Starting at the file's directory works even without package __init__.py.
            cwd = next((parent.parent for parent in path.parents if parent.name in ("test", "tests")), Path("."))
            argv = ("python", "-m", "unittest", "discover", "-s", path.parent.relative_to(cwd).as_posix())
            commands.add((framework, cwd.as_posix(), argv))
        elif framework == "go test":
            for parent in path.parents:
                if (root / parent / "go.mod").is_file():
                    commands.add((framework, parent.as_posix(), ("go", "test", "./...")))
                    break
    return [
        BaselineTestCommand(framework=framework, working_directory=directory, argv=list(argv))
        for framework, directory, argv in sorted(commands)
    ]


def detect_test_framework_and_files(repo_path: str | Path) -> dict:
    """
    Statically inspects a repository to detect its active test framework,
    relevant test configuration files, and existing test files.

    Never executes repository code.

    Returns:
        dict with keys:
            - test_framework: str (e.g. 'pytest', 'unittest', 'jest', 'vitest', 'mocha', 'go test', 'unknown')
            - test_framework_config_file: str | None (relative path to detected config file)
            - existing_test_files: list[str] (relative paths to pre-existing test files, sorted)
    """
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Repository path does not exist or is not a directory: {repo_path}")

    detected_framework: str = "unknown"
    detected_config_file: str | None = None
    existing_test_files: set[str] = set()

    found_configs: list[tuple[str, str]] = []  # (framework, rel_path)
    imported_frameworks: list[tuple[str, str]] = []

    scanned_count = 0

    for item in root.rglob("*"):
        if scanned_count >= MAX_FILES_TO_SCAN:
            break

        try:
            rel_parts = item.relative_to(root).parts
        except ValueError:
            continue

        if any(part in IGNORED_DIRS for part in rel_parts):
            continue

        if not item.is_file():
            continue

        suffix = item.suffix.lower()
        if suffix in IGNORED_EXTENSIONS:
            continue

        scanned_count += 1
        name_lower = item.name.lower()
        rel_str = "/".join(rel_parts)

        # 1. Identify test files
        if _is_test_file(rel_str, name_lower, suffix):
            existing_test_files.add(rel_str)
            # Lightweight content check for test framework signature if safe size
            if suffix == ".py":
                content_sample = _safe_read_text(item, max_chars=1000).lower()
                if "import pytest" in content_sample or "from pytest" in content_sample:
                    imported_frameworks.append(("pytest", rel_str))
                elif "import unittest" in content_sample or "unittest.testcase" in content_sample:
                    imported_frameworks.append(("unittest", rel_str))
            elif suffix == ".go":
                imported_frameworks.append(("go test", rel_str))

        # 2. Check dedicated test config filenames
        if name_lower in TEST_CONFIG_FILENAMES:
            framework = TEST_CONFIG_FILENAMES[name_lower]
            found_configs.append((framework, rel_str))

        # 3. Check multi-purpose config files for test sections
        elif name_lower == "package.json":
            content = _safe_read_text(item)
            try:
                pkg_data = json.loads(content)
                all_deps = {
                    **pkg_data.get("dependencies", {}),
                    **pkg_data.get("devDependencies", {}),
                }
                deps_lower = {k.lower() for k in all_deps.keys()}
                test_script = str(pkg_data.get("scripts", {}).get("test", "")).lower()

                # Prioritize explicit dependencies, or test script containing the exact runner command
                if "vitest" in deps_lower or re.search(r"\bvitest\b", test_script):
                    found_configs.append(("vitest", rel_str))
                elif "jest" in deps_lower or re.search(r"\bjest\b", test_script):
                    found_configs.append(("jest", rel_str))
                elif "mocha" in deps_lower or re.search(r"\bmocha\b", test_script):
                    found_configs.append(("mocha", rel_str))
            except Exception:
                pass

        elif name_lower in ("pyproject.toml", "setup.cfg"):
            content = _safe_read_text(item).lower()
            if (
                "[tool.pytest.ini_options]" in content
                or "[tool.pytest]" in content
                or "[tool:pytest]" in content
                or "[pytest]" in content
                or "pytest" in content
            ):
                found_configs.append(("pytest", rel_str))

        elif name_lower in ("requirements.txt", "pipfile"):
            content = _safe_read_text(item).lower()
            if "pytest" in content:
                found_configs.append(("pytest", rel_str))

    # Priority 1: Dedicated or declared configuration file
    if found_configs:
        detected_framework, detected_config_file = min(found_configs, key=lambda item: item[1])
    # Priority 2: Framework inferred from test file contents
    elif imported_frameworks:
        detected_framework = min(imported_frameworks, key=lambda item: item[1])[0]
    # Priority 3: Fallback if test files exist but no framework declared
    elif existing_test_files:
        has_py = any(f.endswith(".py") for f in existing_test_files)
        has_js = any(f.endswith((".js", ".ts", ".jsx", ".tsx")) for f in existing_test_files)
        has_go = any(f.endswith(".go") for f in existing_test_files)
        if has_py:
            detected_framework = "pytest"
        elif has_js:
            detected_framework = "jest"
        elif has_go:
            detected_framework = "go test"

    return {
        "test_framework": detected_framework,
        "test_frameworks": sorted(
            {framework for framework, _ in found_configs + imported_frameworks}
        ),
        "test_framework_config_file": detected_config_file,
        "existing_test_files": sorted(existing_test_files),
        "baseline_test_commands": _baseline_test_commands(root, found_configs, existing_test_files, imported_frameworks),
    }
