"""Local, write-once repository archives with verified reads and isolated restoration."""

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from app.schemas.repo import RepoAnalysisResponse, SnapshotRecord
from app.services.archive_service import CHUNK_SIZE, MAX_ZIP_SIZE_BYTES, safe_extract_zip

_SNAPSHOT_ID = re.compile(r"snap_[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_ARCHIVE_NAME = "source.zip"


class SnapshotError(Exception):
    """A snapshot reference could not be verified or used."""


class SnapshotNotFound(SnapshotError):
    pass


class SnapshotIntegrityError(SnapshotError):
    pass


class SnapshotStorageError(SnapshotError):
    pass


def _store_path() -> Path:
    if configured := os.getenv("CTF_SNAPSHOT_DIR"):
        return Path(configured).absolute()
    if os.name == "nt":
        data_home = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return data_home / "CodingTaskFactory" / "snapshots"
    data_home = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "coding-task-factory" / "snapshots"


def _snapshot_id(source_type: str, identity: str, commit_sha: str | None, digest: str) -> str:
    payload = json.dumps([1, source_type, identity, commit_sha, digest], separators=(",", ":"))
    return "snap_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _check_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or _is_link(info):
        raise SnapshotIntegrityError("Snapshot directory is not a regular directory.")


@contextmanager
def _open_stored_file(path: Path) -> Iterator[BinaryIO]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or _is_link(before) or before.st_nlink != 1:
        raise SnapshotIntegrityError("Snapshot contains an unsafe file or link.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        if not os.path.samestat(before, os.fstat(source.fileno())):
            raise SnapshotIntegrityError("Snapshot file changed while it was being opened.")
        yield source


def _hash_archive(source: BinaryIO, copy: BinaryIO | None = None) -> str:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        size += len(chunk)
        if size > MAX_ZIP_SIZE_BYTES:
            raise SnapshotIntegrityError("Snapshot archive exceeds the size limit.")
        digest.update(chunk)
        if copy is not None:
            copy.write(chunk)
    return digest.hexdigest()


def publish_snapshot(
    archive: Path,
    analysis: RepoAnalysisResponse,
    *,
    github_identity: str | None = None,
    commit_sha: str | None = None,
) -> RepoAnalysisResponse:
    """Publish a validated intake ZIP and its analysis together via a same-volume rename."""
    if analysis.source_type == "github":
        if not github_identity or not commit_sha or not _COMMIT_SHA.fullmatch(commit_sha):
            raise SnapshotError("GitHub snapshots require a resolved repository and commit.")
    elif github_identity is not None or commit_sha is not None:
        raise SnapshotError("An uploaded archive cannot assert GitHub provenance.")

    root = _store_path()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_directory(root)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=root) as staging:
            entry = Path(staging) / "entry"
            entry.mkdir(mode=0o700)
            with archive.open("rb") as source, (entry / _ARCHIVE_NAME).open("xb") as target:
                digest = _hash_archive(source, target)

            identity = github_identity or f"zip:sha256:{digest}"
            snapshot_id = _snapshot_id(analysis.source_type, identity, commit_sha, digest)
            result = analysis.model_copy(update={
                "snapshot_id": snapshot_id,
                "content_sha256": digest,
                "repository_identity": identity,
                "commit_sha": commit_sha,
                "retrieved_at": datetime.now(timezone.utc),
            })
            record = SnapshotRecord(
                analysis=result,
                archive_location=f"{snapshot_id}/{_ARCHIVE_NAME}",
            )
            metadata = record.model_dump_json().encode("utf-8")
            if len(metadata) > _MAX_METADATA_BYTES:
                raise SnapshotError("Snapshot metadata exceeds the size limit.")
            (entry / "metadata.json").write_bytes(metadata)
            destination = root / snapshot_id
            if os.path.lexists(destination):
                return load_snapshot(snapshot_id).analysis
            try:
                entry.rename(destination)
            except OSError:
                if not os.path.lexists(destination):
                    raise
                # A concurrent or earlier submission won. Never overwrite its record.
                return load_snapshot(snapshot_id).analysis
            return result
    except OSError as exc:
        raise SnapshotStorageError("Snapshot storage is unavailable.") from exc


def _read_record(snapshot_id: str) -> tuple[Path, SnapshotRecord]:
    if not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise SnapshotError("Invalid repository snapshot ID.")
    root = _store_path()
    directory = root / snapshot_id
    try:
        _check_directory(root)
        _check_directory(directory)
    except FileNotFoundError as exc:
        raise SnapshotNotFound("Repository snapshot was not found.") from exc

    with _open_stored_file(directory / "metadata.json") as source:
        metadata = source.read(_MAX_METADATA_BYTES + 1)
    if len(metadata) > _MAX_METADATA_BYTES:
        raise SnapshotIntegrityError("Snapshot metadata exceeds the size limit.")
    record = SnapshotRecord.model_validate_json(metadata)
    analysis = record.analysis
    identity = analysis.repository_identity
    digest = analysis.content_sha256
    sha = analysis.commit_sha
    if not identity or not digest or not analysis.retrieved_at:
        raise SnapshotIntegrityError("Snapshot provenance is incomplete.")
    if analysis.source_type == "zip":
        if identity != f"zip:sha256:{digest}" or sha is not None:
            raise SnapshotIntegrityError("Uploaded snapshot provenance does not match.")
    elif not sha or not _COMMIT_SHA.fullmatch(sha):
        raise SnapshotIntegrityError("GitHub snapshot commit is invalid.")
    if (
        analysis.snapshot_id != snapshot_id
        or _snapshot_id(analysis.source_type, identity, sha, digest) != snapshot_id
        or record.archive_location != f"{snapshot_id}/{_ARCHIVE_NAME}"
    ):
        raise SnapshotIntegrityError("Snapshot metadata does not match its ID.")
    return directory / _ARCHIVE_NAME, record


def _verify_snapshot(snapshot_id: str, copy: BinaryIO | None = None) -> SnapshotRecord:
    try:
        archive, record = _read_record(snapshot_id)
        with _open_stored_file(archive) as source:
            if _hash_archive(source, copy) != record.analysis.content_sha256:
                raise SnapshotIntegrityError("Snapshot archive checksum does not match.")
        return record
    except (OSError, ValueError) as exc:
        raise SnapshotIntegrityError("Snapshot metadata or archive is unreadable or corrupt.") from exc


def load_snapshot(snapshot_id: str) -> SnapshotRecord:
    """Load server-owned metadata and verify the entire stored archive."""
    return _verify_snapshot(snapshot_id)


def matching_snapshot_id(*snapshot_ids: str | None) -> str | None:
    ids = {snapshot_id for snapshot_id in snapshot_ids if snapshot_id is not None}
    if len(ids) > 1:
        raise SnapshotError("Artifacts reference different repository snapshots.")
    return ids.pop() if ids else None


def load_matching_snapshot(*snapshot_ids: str | None) -> SnapshotRecord | None:
    """Resolve one authoritative snapshot for artifacts that may omit provenance."""
    snapshot_id = matching_snapshot_id(*snapshot_ids)
    return load_snapshot(snapshot_id) if snapshot_id is not None else None


@contextmanager
def restore_snapshot(snapshot_id: str) -> Iterator[tuple[Path, SnapshotRecord]]:
    """Yield a new disposable source root, extracted from the exact verified byte stream."""
    with tempfile.TemporaryDirectory(prefix="ctf_snapshot_") as temporary:
        workspace = Path(temporary)
        archive = workspace / _ARCHIVE_NAME
        with archive.open("xb") as copy:
            record = _verify_snapshot(snapshot_id, copy)
        sources = workspace / "sources"
        sources.mkdir()
        try:
            root = safe_extract_zip(archive, sources)
        except (OSError, ValueError) as exc:
            raise SnapshotIntegrityError("Stored snapshot cannot be safely restored.") from exc
        archive.unlink()
        yield root, record
