"""Operator-approved, offline Python dependency policy for sandbox jobs."""

import hashlib
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from app.services.solution_changes import solution_path_parts

MAX_WHEEL_BYTES = 96 * 1024 * 1024
_LOCK_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;@]+) --hash=sha256:([a-f0-9]{64})")
_TEST_GROUPS = {"test", "tests", "qa"}


class DependencyPolicyError(ValueError):
    pass


def _text(path: Path, limit: int = 128 * 1024) -> str:
    try:
        with path.open("rb") as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError
        return data.decode("utf-8")
    except (OSError, ValueError) as exc:
        raise DependencyPolicyError("A dependency manifest or approved lock cannot be read safely.") from exc


def _ordinary_file(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


@dataclass(frozen=True)
class DependencyBundle:
    lock: bytes = b""
    wheels: tuple[Path, ...] = ()
    pins: tuple[tuple[str, str], ...] = ()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.lock).hexdigest()


def configured_bundle(directory: str | None) -> DependencyBundle:
    if not directory:
        return DependencyBundle()
    root = Path(directory)
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise DependencyPolicyError("The configured wheelhouse must be an absolute, ordinary directory.")
        lock_file = root / "requirements.lock"
        if not _ordinary_file(lock_file):
            raise DependencyPolicyError("The approved dependency lock must be an ordinary file.")
        lines = [line.strip() for line in _text(lock_file).splitlines() if line.strip() and not line.strip().startswith("#")]
        if len(lines) > 100:
            raise DependencyPolicyError("The approved lock exceeds the package count limit.")
        entries = {}
        hashes = {}
        for line in lines:
            match = _LOCK_LINE.fullmatch(line)
            if not match:
                raise DependencyPolicyError("The approved lock accepts only exact pins with one SHA-256 hash per distribution.")
            name, version, digest = match.groups()
            name = canonicalize_name(name)
            Requirement(f"{name}=={version}")
            if name in entries or "*" in version:
                raise DependencyPolicyError("The approved lock contains duplicate or inexact pins.")
            entries[name] = version
            hashes[name] = digest
        selected: dict[str, Path] = {}
        size = 0
        for entry in sorted(root.glob("*.whl")):
            name, version, _, _ = parse_wheel_filename(entry.name)
            if name not in entries or str(version) != entries[name]:
                continue
            if not _ordinary_file(entry):
                raise DependencyPolicyError("Approved wheel files must be regular files.")
            size += entry.stat().st_size
            if size > MAX_WHEEL_BYTES:
                raise DependencyPolicyError("The approved wheelhouse exceeds its compressed size limit.")
            with entry.open("rb") as wheel:
                if hashlib.file_digest(wheel, "sha256").hexdigest() == hashes[name]:
                    selected[name] = entry
        if selected.keys() != entries.keys():
            raise DependencyPolicyError("The wheelhouse is missing an exact SHA-256-matching wheel.")
        return DependencyBundle(
            ("\n".join(lines) + "\n").encode() if lines else b"",
            tuple(selected[name] for name in sorted(selected)), tuple(sorted(entries.items())),
        )
    except (OSError, InvalidRequirement, ValueError) as exc:
        if isinstance(exc, DependencyPolicyError):
            raise
        raise DependencyPolicyError("The operator-approved offline wheelhouse is invalid or unavailable.") from exc


def verify_project_dependencies(root: Path, framework: str, bundle: DependencyBundle) -> None:
    """Interpret declarations as data. Only the operator's lock reaches pip."""
    pins = dict(bundle.pins)
    declarations = ["pytest"] if framework == "pytest" else []
    manifests = set(root.glob("requirements*.txt")) | set(root.glob("requirements/*.txt"))
    pending = list(sorted(manifests))
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for line in _text(path).splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith(("-r ", "--requirement ")):
                try:
                    parts = solution_path_parts(line.split(maxsplit=1)[1])
                    included = path.parent.joinpath(*parts).resolve(strict=True)
                    if not included.is_relative_to(root) or not included.is_file() or included.suffix != ".txt":
                        raise ValueError
                except (OSError, ValueError) as exc:
                    raise DependencyPolicyError("A requirements include escapes the project or is unsupported.") from exc
                pending.append(included)
                continue
            declarations.append(line)

    config = root / "pyproject.toml"
    if config.is_file():
        try:
            data = tomllib.loads(_text(config))
            project = data.get("project", {})
            if "dependencies" in project.get("dynamic", []) or data.get("tool", {}).get("poetry", {}).get("dependencies"):
                raise ValueError
            declarations.extend(project.get("dependencies", []))
            groups = (project.get("optional-dependencies", {}), data.get("dependency-groups", {}))
            group_names = _TEST_GROUPS if any(_TEST_GROUPS.intersection(group) for group in groups) else {"dev"}
            for group in groups:
                for name in group_names.intersection(group):
                    declarations.extend(group[name])
        except (ValueError, TypeError, AttributeError) as exc:
            raise DependencyPolicyError("Project dependency declarations require supported static Python manifests.") from exc
    if any((root / name).is_file() for name in ("Pipfile", "setup.py", "setup.cfg")) and not manifests and not config.is_file():
        raise DependencyPolicyError("This project's dependency declarations require an explicit supported requirements manifest.")

    for declaration in declarations:
        if not isinstance(declaration, str):
            raise DependencyPolicyError("A dependency group contains unsupported directives.")
        try:
            requirement = Requirement(declaration)
        except InvalidRequirement as exc:
            raise DependencyPolicyError("VCS URLs, installer options, and unsupported dependency expressions are forbidden.") from exc
        version = pins.get(canonicalize_name(requirement.name))
        if requirement.url or version is None or not requirement.specifier.contains(version, prereleases=True):
            raise DependencyPolicyError("A declared project dependency lacks an approved compatible offline pin.")
