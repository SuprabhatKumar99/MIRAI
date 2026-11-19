"""Keep task solutions out of the starting repository's test harness."""

import ast
import fnmatch
import tomllib
from configparser import ConfigParser, Error as ConfigError
from pathlib import Path, PurePosixPath

from app.services.solution_changes import solution_path_parts

TEST_DIRECTORIES = {"test", "tests", "__tests__", "spec", "specs"}
_RUNNER_FILES = {"conftest.py", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml",
                 "tox.ini", "jest.config.js", "jest.config.ts", "vitest.config.ts", "vitest.config.js"}
PYTEST_CONFIGS = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
MAX_MANIFEST_BYTES = 128 * 1024
_TEST_SECTIONS = ("[tool.pytest", "[tool:pytest]")


def read_manifest(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        return ""


def pytest_settings(directory: Path) -> tuple[str | None, dict]:
    """Read supported static pytest configuration without importing the project."""
    for name in PYTEST_CONFIGS:
        text = read_manifest(directory / name)
        if not text:
            if (directory / name).is_file() and name in ("pytest.ini", ".pytest.ini"):
                return name, {}
            continue
        try:
            if name == "pyproject.toml":
                section = tomllib.loads(text).get("tool", {}).get("pytest", {})
                if isinstance(section, dict) and section:
                    options = section.get("ini_options", section)
                    return name, options if isinstance(options, dict) else {}
            else:
                config = ConfigParser(interpolation=None)
                config.read_string(text)
                section = "tool:pytest" if name == "setup.cfg" else "pytest"
                if config.has_section(section):
                    return name, dict(config[section])
                if name in ("pytest.ini", ".pytest.ini"):
                    return name, {}
        except (ValueError, ConfigError, TypeError, AttributeError):
            continue
    return None, {}


def setting_words(value: object, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split()) or default
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value) or default
    return default


def _defines_tests(text: str, prefixes: tuple[str, ...] = ("test",)) -> bool:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return True  # Preserve a candidate we cannot reliably classify.
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(fnmatch.fnmatchcase(node.name, prefix) or node.name.startswith(prefix) for prefix in prefixes)
        for node in ast.walk(tree)
    )


def _declares_runner(name: str, content: str) -> bool:
    if name == "pyproject.toml":
        try:
            return "pytest" in tomllib.loads(content).get("tool", {})
        except (ValueError, TypeError, AttributeError):
            return any(section in content.casefold() for section in _TEST_SECTIONS)
    if name == "setup.cfg":
        configuration = ConfigParser(interpolation=None)
        try:
            configuration.read_string(content)
            return configuration.has_section("tool:pytest")
        except ConfigError:
            return any(section in content.casefold() for section in _TEST_SECTIONS)
    return False


def protect_regression_paths(
    root: Path, original_paths: list[str], change_paths: list[str], replacements: dict[str, str] | None = None
) -> None:
    """Reject edits to original tests, test assets, or test runner configuration."""
    known = {
        path.casefold() for path in original_paths
        if not path.lower().endswith(".py") or _defines_tests(read_manifest(root / path))
    }
    # Native configurations may choose test paths outside conventional test folders.
    settings_by_directory = []
    dirs = {root}
    for path in root.rglob("*"):
        if path.name in PYTEST_CONFIGS and path.is_file():
            dirs.add(path.parent)
    for directory in dirs:
        name, options = pytest_settings(directory)
        if not name:
            continue
        config_path = (directory / name).relative_to(root).as_posix().casefold()
        known.add(config_path)
        paths = setting_words(options.get("testpaths"))
        patterns = setting_words(options.get("python_files"), ("test_*.py", "*_test.py"))
        functions = setting_words(options.get("python_functions"), ("test",))
        settings_by_directory.append((directory.relative_to(root).as_posix(), paths, patterns, functions))

    for change in change_paths:
        parts = solution_path_parts(change.casefold())
        path = PurePosixPath(*parts)
        name = path.name
        prior = read_manifest(root / change) if name.endswith(".py") else ""
        replacement = replacements.get(change, "") if replacements else ""
        protected = (
            path.as_posix() in known
            or any(part in TEST_DIRECTORIES for part in parts[:-1])
            or name in _RUNNER_FILES
            or (name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
                and (_defines_tests(prior) or _defines_tests(replacement)))
            or _declares_runner(name, replacement)
        )
        for directory, paths, patterns, functions in settings_by_directory:
            base = PurePosixPath(directory.casefold())
            if not path.is_relative_to(base):
                continue
            relative = path.relative_to(base).as_posix()
            protected |= any(
                relative == test_path.casefold().rstrip("/")
                or relative.startswith(test_path.casefold().rstrip("/") + "/")
                for test_path in paths
            ) or (
                any(fnmatch.fnmatchcase(name, pattern.casefold()) for pattern in patterns)
                and (_defines_tests(prior, functions) or _defines_tests(replacement, functions))
            )
        if protected:
            raise ValueError("Reference changes cannot modify the starting test harness.")
