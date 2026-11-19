"""Bounded ZIP extraction into an empty, service-owned directory."""

import re
import stat
import unicodedata
import zipfile
import zlib
from pathlib import Path

MAX_ZIP_SIZE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_SIZE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_FILE_COUNT = 1500
MAX_SINGLE_FILE_SIZE_BYTES = 10 * 1024 * 1024
MAX_COMPRESSION_RATIO = 20
CHUNK_SIZE = 64 * 1024

_DEVICE_NAME = re.compile(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³]", re.I)
_UNSAFE_CHARACTER = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')


def portable_path_parts(path: str) -> tuple[str, ...]:
    """Reject traversal, Windows aliases and filesystem-specific escape syntax."""
    parts = tuple(path.replace("\\", "/").split("/"))
    if len(path) > 1024 or len(parts) > 100:
        raise ValueError("Repository path exceeds supported length or depth.")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError(f"Path traversal or absolute path is forbidden: {path!r}")
        if (
            part.endswith((" ", "."))
            or _UNSAFE_CHARACTER.search(part)
            or _DEVICE_NAME.fullmatch(part.split(".", 1)[0].rstrip())
        ):
            raise ValueError(f"Unsafe repository path: {path!r}")
    return parts


def _plan_entries(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]]:
    entries = archive.infolist()
    if len(entries) > MAX_EXTRACTED_FILE_COUNT:
        raise ValueError("ZIP archive exceeds the entry count limit.")

    plan = []
    # Track implicit parents too, including case aliases and file/directory conflicts.
    paths: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}
    explicit_paths: set[tuple[str, ...]] = set()
    total_size = 0
    for info in entries:
        original = info.orig_filename
        if info.flag_bits & 1:
            raise ValueError("Encrypted ZIP entries are not permitted.")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind == stat.S_IFLNK:
            raise ValueError("Symbolic links in ZIP archives are forbidden.")
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError("Special files in ZIP archives are forbidden.")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError("Unsupported ZIP compression method.")

        is_dir = info.is_dir()
        if (is_dir and info.file_size) or (kind == stat.S_IFDIR and not is_dir):
            raise ValueError("Invalid ZIP directory entry.")
        parts = portable_path_parts(original[:-1] if is_dir else original)
        key = tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)
        if key in explicit_paths:
            raise ValueError("Duplicate or case-colliding ZIP path.")
        for length in range(1, len(parts) + 1):
            entry = (parts[:length], length < len(parts) or is_dir)
            if paths.setdefault(key[:length], entry) != entry:
                raise ValueError("Conflicting ZIP paths.")
        explicit_paths.add(key)

        total_size += info.file_size
        if info.file_size > MAX_SINGLE_FILE_SIZE_BYTES or total_size > MAX_EXTRACTED_SIZE_BYTES:
            raise ValueError("ZIP archive exceeds the uncompressed size limit.")
        if info.file_size > 50 * 1024 and info.file_size > MAX_COMPRESSION_RATIO * info.compress_size:
            raise ValueError("Suspicious compression ratio (potential ZIP bomb).")
        plan.append((info, parts, is_dir))
    return plan


def safe_extract_zip(zip_path: Path, extract_dir: Path) -> Path:
    """Preflight paths and sizes before reading entries, then check CRC during extraction."""
    if not extract_dir.is_dir() or extract_dir.is_symlink() or any(extract_dir.iterdir()):
        raise ValueError("ZIP extraction requires a fresh, empty workspace.")
    root = extract_dir.resolve(strict=True)
    if zip_path.stat().st_size > MAX_ZIP_SIZE_BYTES:
        raise ValueError("Archive file exceeds the compressed size limit.")
    with zip_path.open("rb") as source:
        if source.read(4) not in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
            raise ValueError("File does not have a valid ZIP magic byte header.")
        try:
            with zipfile.ZipFile(source) as archive:
                for info, parts, is_dir in _plan_entries(archive):
                    target = root.joinpath(*parts)
                    resolved = target.resolve()
                    if not resolved.is_relative_to(root):
                        raise ValueError("ZIP path escapes the extraction workspace.")
                    if resolved.relative_to(root).parts != parts:
                        raise ValueError("ZIP path aliases another filesystem path.")
                    if is_dir:
                        # Reading the entry also checks the directory's local header.
                        with archive.open(info) as entry:
                            entry.read(1)
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as entry, target.open("xb") as output:
                        remaining = info.file_size
                        while chunk := entry.read(CHUNK_SIZE):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise ValueError("ZIP entry exceeds its declared size.")
                            output.write(chunk)
                        if remaining:
                            raise ValueError("ZIP entry is shorter than its declared size.")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
            raise ValueError("Malformed or corrupt ZIP archive.") from exc

    entries = list(root.iterdir())
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else root
