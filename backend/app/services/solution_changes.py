"""Validate and apply complete file replacements in a disposable workspace."""

import hashlib
import json
import os
import re
import stat
import tomllib
import unicodedata
from pathlib import Path

from app.schemas.solution import SolutionFile
from app.services.archive_service import portable_path_parts

MAX_SOLUTION_FILES = 12
MAX_REPLACEMENT_BYTES = 24 * 1024
_PRIVATE_FILE_NAMES = {
    ".git-credentials", ".netrc", ".npmrc", ".pypirc", "credentials.json",
    "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml",
}
_PRIVATE_DIRS = {".git", ".aws", ".ssh", ".gnupg", ".gcloud", ".azure"}
_PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks"}
_PRIVATE_TEXT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{35}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,})\b"
)
_PATCH_HEADER = re.compile(r"^(?:diff --git |--- a/|@@ -\d)", re.M)


class SolutionChangeError(ValueError):
    """A candidate change fails a structural or filesystem policy check."""


def has_secret_marker(text: str) -> bool:
    return bool(_PRIVATE_TEXT.search(text))


def is_protected_path(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    name = lowered[-1]
    return (
        any(part in _PRIVATE_DIRS for part in lowered)
        or name in _PRIVATE_FILE_NAMES
        or name == ".env"
        or name.startswith((".env.", "id_rsa", "id_ed25519"))
        or Path(name).suffix in _PRIVATE_SUFFIXES
    )


def _path_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)


def solution_path_parts(relative_path: str) -> tuple[str, ...]:
    if not relative_path.strip():
        raise SolutionChangeError("Relative path cannot be empty.")
    if relative_path.startswith(("/", "\\")) or (len(relative_path) > 1 and relative_path[1] == ":"):
        raise SolutionChangeError("Absolute paths are forbidden.")
    if "\\" in relative_path:
        raise SolutionChangeError("Use forward slashes in repository-relative paths.")
    try:
        parts = portable_path_parts(relative_path)
        if any(len(part.encode("utf-8")) > 255 for part in parts):
            raise ValueError("Path segment is too long")
    except ValueError as exc:
        raise SolutionChangeError("Path traversal or unsafe repository path.") from exc
    if is_protected_path(parts):
        raise SolutionChangeError("Changes to protected repository paths are forbidden.")
    return parts


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def resolve_safe_workspace_path(workspace_root: Path | str, relative_path: str) -> Path:
    """Resolve an exact portable spelling and reject file aliases and linked paths."""
    root = Path(workspace_root).resolve(strict=True)
    parts = solution_path_parts(relative_path)
    target = root
    for index, part in enumerate(parts):
        if target.is_dir() and any(
            item.name != part and _path_key((item.name,)) == _path_key((part,))
            for item in target.iterdir()
        ):
            raise SolutionChangeError("Path aliases an existing repository path.")
        target /= part
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise SolutionChangeError("Symlinks, reparse points and hardlinks are forbidden.")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise SolutionChangeError("Path has a non-directory parent.")
    resolved = target.resolve()
    if not resolved.is_relative_to(root) or resolved.relative_to(root).parts != parts:
        raise SolutionChangeError("Path escapes or aliases a workspace path.")
    return target


def check_change_paths(files: list[SolutionFile]) -> None:
    """Reject duplicates, aliases and conflicting parent/child changes before any write."""
    leaf_keys: set[tuple[str, ...]] = set()
    seen_names: dict[tuple[str, ...], tuple[str, ...]] = {}
    if len(files) > MAX_SOLUTION_FILES:
        raise SolutionChangeError("Too many file changes.")
    for file in files:
        parts = solution_path_parts(file.path)
        key = _path_key(parts)
        if key in leaf_keys:
            raise SolutionChangeError("Duplicate or case-colliding file changes.")
        leaf_keys.add(key)
        for length in range(1, len(parts) + 1):
            if seen_names.setdefault(key[:length], parts[:length]) != parts[:length]:
                raise SolutionChangeError("File changes use conflicting directory spellings.")
    if any(key[:length] in leaf_keys for key in leaf_keys for length in range(1, len(key))):
        raise SolutionChangeError("File changes conflict with a parent or child path.")


def _check_replacement(file: SolutionFile, data: bytes) -> None:
    if len(data) > MAX_REPLACEMENT_BYTES or b"\0" in data:
        raise SolutionChangeError("A replacement exceeds the size limit or contains binary content.")
    if not data.strip() and Path(file.path).name != "__init__.py":
        raise SolutionChangeError("A replacement must contain complete file content.")
    if has_secret_marker(file.code):
        raise SolutionChangeError("A replacement contains credential-like material.")
    name = Path(file.path).name.casefold()
    if _PATCH_HEADER.match(file.code.lstrip()) or (
        not name.endswith((".md", ".txt")) and file.code.lstrip().startswith("```")
    ):
        raise SolutionChangeError("Return complete file content instead of a diff or code fence.")
    try:
        if name.endswith((".py", ".pyi")):
            compile(data, "<candidate>", "exec", dont_inherit=True)
        elif name.endswith(".toml"):
            tomllib.loads(file.code)
        elif name in {"package.json", "package-lock.json", "composer.json"}:
            if not isinstance(json.loads(file.code), dict):
                raise ValueError("Expected a JSON object")
    except (ValueError, SyntaxError, RecursionError, OverflowError) as exc:
        raise SolutionChangeError("A replacement fails its static syntax check.") from exc


def apply_changes(
    workspace_root: Path | str,
    files: list[SolutionFile],
    *,
    authorized_deletions: frozenset[str] = frozenset(),
    check_content: bool = False,
) -> list[SolutionFile]:
    """Preflight the entire change set, then write exact bytes and return server checksums."""
    check_change_paths(files)
    planned: list[tuple[SolutionFile, Path, bytes]] = []
    for file in files:
        target = resolve_safe_workspace_path(workspace_root, file.path)
        base_hash = None
        original = None
        if file.status == "create":
            if target.exists():
                raise SolutionChangeError("A create target file already exists.")
        else:
            if not target.is_file():
                raise SolutionChangeError("A modify or delete target does not exist as a regular file.")
            if file.status == "delete" and file.path not in authorized_deletions:
                raise SolutionChangeError("A deletion is missing explicit path authorization.")
            if target.stat().st_size > MAX_REPLACEMENT_BYTES:
                raise SolutionChangeError("An existing target exceeds the file-change size limit.")
            original = target.read_bytes()
            base_hash = hashlib.sha256(original).hexdigest()
        if file.base_sha256 is not None and file.base_sha256 != base_hash:
            raise SolutionChangeError("A base checksum does not match the workspace.")

        try:
            replacement = file.code.encode("utf-8")
        except UnicodeError as exc:
            raise SolutionChangeError("A replacement contains invalid Unicode.") from exc
        result_hash = None
        if file.status != "delete":
            result_hash = hashlib.sha256(replacement).hexdigest()
            if check_content:
                _check_replacement(file, replacement)
                if original == replacement:
                    raise SolutionChangeError("A proposed modification leaves the file unchanged.")
        if file.result_sha256 is not None and file.result_sha256 != result_hash:
            raise SolutionChangeError("A result checksum does not match the replacement.")
        planned.append((file.model_copy(update={
            "base_sha256": base_hash, "result_sha256": result_hash,
        }), target, replacement))

    for file, target, replacement in planned:
        if file.status == "delete":
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb" if file.status == "create" else "wb") as output:
                output.write(replacement)
    return [file for file, _, _ in planned]
