"""Select bounded, deterministic source context from a restored repository."""

import ast
import json
import os
import posixpath
import re
from pathlib import Path, PurePosixPath

from app.schemas.solution import SolutionContextFile
from app.services.repo_service import IGNORED_DIRS
from app.services.solution_changes import has_secret_marker, is_protected_path

MAX_CONTEXT_FILES = 18
MAX_CONTEXT_FILE_BYTES = 12 * 1024
EXCERPT_BYTES = 2 * 1024
_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".cs", ".c", ".cpp", ".h", ".hpp", ".rb", ".php",
    ".sql", ".sh", ".ps1", ".html", ".css", ".scss", ".swift", ".ex", ".exs",
}
_MANIFESTS = {
    "dockerfile", "makefile", "justfile", "gemfile", "gemfile.lock", "pipfile", "pipfile.lock",
    "poetry.lock", "uv.lock", "yarn.lock", "go.mod", "go.sum", "cargo.lock", "gradlew",
    "setup.cfg", "tox.ini", ".python-version", ".node-version", ".gitignore",
}
_CONFIG_SUFFIXES = {".json", ".toml", ".yaml", ".yml", ".ini", ".xml", ".gradle", ".csproj"}
_JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)['"]([^'"]+)['"]""")
_STOP_WORDS = {"app", "the", "for", "and", "use", "with", "this", "add", "test", "tests", "file", "new"}


class RepositoryContextFile(SolutionContextFile):
    text: str


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.casefold())) - _STOP_WORDS


def _inventory(root: Path, existing_tests: set[str]) -> dict[str, SolutionContextFile]:
    files = {}
    for folder, dirs, names in os.walk(root):
        directory = Path(folder)
        dirs[:] = [
            name for name in dirs
            if name not in IGNORED_DIRS
            and not is_protected_path((directory / name).relative_to(root).parts)
            and not (directory / name).is_symlink()
        ]
        for name in names:
            path = directory / name
            parts = path.relative_to(root).parts
            if path.is_symlink() or is_protected_path(parts) or has_secret_marker(name):
                continue
            relative = "/".join(parts)
            lowered = name.casefold()
            if lowered.startswith("readme"):
                kind = "readme"
            elif lowered in _MANIFESTS or path.suffix.lower() in _CONFIG_SUFFIXES or (
                lowered.startswith("requirements") and lowered.endswith(".txt")
            ):
                kind = "config"
            elif relative in existing_tests or lowered.startswith("test_") or any(
                marker in lowered for marker in ("_test.", ".test.", ".spec.")
            ):
                kind = "test"
            elif path.suffix.lower() in _SOURCE_SUFFIXES or lowered.endswith((".md", ".txt")):
                kind = "source"
            else:
                continue
            files[relative] = SolutionContextFile(path=relative, kind=kind, complete=False)
    return files


def _read_entry(root: Path, entry: SolutionContextFile) -> RepositoryContextFile | None:
    with (root / entry.path).open("rb") as source:
        content = source.read(MAX_CONTEXT_FILE_BYTES + 1)
    complete = len(content) <= MAX_CONTEXT_FILE_BYTES
    if b"\x00" in content:
        return None
    try:
        text = content.decode("utf-8") if complete else content[:EXCERPT_BYTES].decode("utf-8", "ignore")
    except UnicodeDecodeError:
        return None
    if has_secret_marker(text):
        return None
    return RepositoryContextFile(path=entry.path, kind=entry.kind, complete=complete, text=text)


def _related_paths(entry: RepositoryContextFile, inventory: dict[str, SolutionContextFile]) -> set[str]:
    path = PurePosixPath(entry.path)
    if path.suffix in (".py", ".pyi"):
        try:
            nodes = ast.walk(ast.parse(entry.text))
        except (SyntaxError, ValueError, RecursionError):
            return set()
        modules = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                modules.update(alias.name.replace(".", "/") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level > len(path.parts):
                    continue
                prefix = path.parts[:-node.level] if node.level else ()
                module = "/".join((*prefix, *(node.module or "").split("."))).strip("/")
                if module:
                    modules.add(module)
                modules.update(f"{module}/{alias.name}".strip("/") for alias in node.names)
        targets = {
            "/" + module + suffix
            for module in modules if module
            for suffix in (".py", ".pyi", "/__init__.py", "/__init__.pyi")
        }
        return {name for name in inventory if any(("/" + name).endswith(target) for target in targets)}
    if path.suffix in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte"):
        targets = {
            posixpath.normpath(posixpath.join(path.parent.as_posix(), imported))
            for imported in _JS_IMPORT.findall(entry.text) if imported.startswith("./") or imported.startswith("../")
        }
        return {
            name for name in inventory
            if name in targets or str(PurePosixPath(name).with_suffix("")) in targets
            or (name.endswith(".d.ts") and name.removesuffix(".d.ts") in targets)
            or (PurePosixPath(name).stem == "index" and str(PurePosixPath(name).parent) in targets)
        }
    return set()


def build_repository_context(
    root: Path,
    *,
    relevant_paths: set[str],
    task_text: str,
    existing_tests: list[str],
    budget_bytes: int,
) -> list[RepositoryContextFile]:
    """Rank by task paths, import edges, test relevance, and project configuration."""
    inventory = _inventory(root, set(existing_tests))
    direct = relevant_paths & inventory.keys()
    words = _words(task_text)
    anchors = direct or set(sorted(
        (path for path, item in inventory.items() if item.kind == "source"),
        key=lambda path: (-len(words & _words(path)), path),
    )[:3])
    roots = {PurePosixPath(path).parts[0] for path in anchors if "/" in path}
    feature_words = words | _words(" ".join(anchors))
    related = set()
    read: dict[str, RepositoryContextFile | None] = {}
    for path in sorted(anchors)[:MAX_CONTEXT_FILES]:
        entry = read[path] = _read_entry(root, inventory[path])
        if entry:
            related.update(_related_paths(entry, inventory))

    def rank(path: str) -> tuple[int, str]:
        kind = inventory[path].kind
        matches = len(_words(path) & feature_words)
        score = {"source": 0, "test": 180, "config": 160, "readme": 100}[kind]
        score += min(matches, 8) * 16
        if path in direct:
            score += 2000
        elif path in related:
            score += 800
        elif path in anchors:
            score += 350
        if PurePosixPath(path).parts[0] in roots:
            score += 40
        return -score, path

    selected = []
    remaining = budget_bytes - 2  # JSON array brackets
    for path in sorted(inventory, key=rank):
        if len(selected) >= MAX_CONTEXT_FILES or remaining < 256:
            break
        entry = read[path] if path in read else _read_entry(root, inventory[path])
        if entry is None:
            continue
        size = len(encode_json(entry.model_dump()).encode("utf-8")) + bool(selected)
        if size > remaining and entry.complete:
            entry = entry.model_copy(update={"complete": False, "text": entry.text[:EXCERPT_BYTES]})
            size = len(encode_json(entry.model_dump()).encode("utf-8")) + bool(selected)
        if size <= remaining:
            selected.append(entry)
            remaining -= size
    return selected
