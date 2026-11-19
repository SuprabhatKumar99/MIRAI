"""Static framework adapters for repository-native generated tests."""

import fnmatch
import hashlib
import re
import tomllib
from configparser import ConfigParser, Error as ConfigError
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.schemas.repo import BaselineTestCommand, RepoAnalysisResponse
from app.schemas.test_suite import OriginalRegressionFile, TestDependency
from app.services.regression_protection import (
    TEST_DIRECTORIES, pytest_settings, read_manifest, setting_words,
)
from app.services.repo_service import IGNORED_DIRS
from app.services.solution_changes import is_protected_path, solution_path_parts

_PACKAGE = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9_.-]*)(?=\s*(?:\[|[<>=!~;@]|$))")
_IMPORT_ALIASES = {
    "python-dotenv": "dotenv", "pyyaml": "yaml", "pillow": "PIL",
    "scikit-learn": "sklearn", "opencv-python": "cv2", "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil", "pydantic-settings": "pydantic_settings",
    "pytest-mock": "pytest_mock", "pytest-asyncio": "pytest_asyncio",
}


class TestGenerationInputError(ValueError):
    """The requested native test plan cannot be safely constructed."""


@dataclass
class NativeTestProject:
    scope: PurePosixPath
    framework: str
    config: str | None
    directories: tuple[str, ...]
    file_patterns: tuple[str, ...]
    function_patterns: tuple[str, ...]
    class_patterns: tuple[str, ...]
    dependencies: dict[str, TestDependency]
    modules: dict[str, str]
    test_helper_modules: set[str]
    original_tests: list[OriginalRegressionFile]


@dataclass
class TestInspection:
    cases: dict[str, set[str]]
    dependencies: list[TestDependency]
    new_imports: set[str]


class TestFrameworkAdapter(Protocol):
    """Extension boundary for inspecting, collecting and invoking a native framework."""

    def inspect(self, root: Path, analysis: RepoAnalysisResponse, hints: set[str],
                preferred: str | None) -> NativeTestProject: ...

    def commands(self, project: NativeTestProject, paths: list[str]) -> list[BaselineTestCommand]: ...

    def validate_file(self, root: Path, project: NativeTestProject, *, path: str, code: str,
                      new_modules: dict[str, str]) -> TestInspection: ...


def python_module_paths(paths: set[str], scope: PurePosixPath) -> dict[str, str]:
    modules = {}
    for path in sorted(paths):
        relative = PurePosixPath(path)
        if not relative.is_relative_to(scope) or relative.suffix != ".py":
            continue
        parts = relative.relative_to(scope).with_suffix("").parts
        if parts and parts[0] == "src":
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and all(part.isidentifier() for part in parts):
            modules.setdefault(".".join(parts), path)
    return modules


def _declarations(path: Path) -> list[str]:
    text = read_manifest(path)
    if path.name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
            project = data.get("project", {})
            poetry = data.get("tool", {}).get("poetry", {})
            scopes = [
                project.get("dependencies", []),
                *project.get("optional-dependencies", {}).values(),
                *data.get("dependency-groups", {}).values(),
                poetry.get("dependencies", {}),
                poetry.get("dev-dependencies", {}),
                *(group.get("dependencies", {}) for group in poetry.get("group", {}).values()),
            ]
            return [item for scope in scopes for item in scope if isinstance(item, str)]
        except (ValueError, TypeError, AttributeError):
            return []
    if path.name == "setup.cfg":
        config = ConfigParser(interpolation=None)
        try:
            config.read_string(text)
            requirements = config.get("options", "install_requires", fallback="").splitlines()
            if config.has_section("options.extras_require"):
                requirements.extend(line for _, value in config.items("options.extras_require") for line in value.splitlines())
            return requirements
        except ConfigError:
            return []
    if path.name == "Pipfile":
        try:
            data = tomllib.loads(text)
            return list(data.get("packages", {})) + list(data.get("dev-packages", {}))
        except (ValueError, TypeError):
            return []
    return text.splitlines()


def _python_dependencies(root: Path, scope: PurePosixPath) -> dict[str, TestDependency]:
    directory = root / scope
    manifests = [
        path for path in (
            *(directory / name for name in ("pyproject.toml", "setup.cfg", "Pipfile")),
            *directory.glob("requirements*.txt"),
            *directory.glob("requirements/*.txt"),
        ) if path.is_file()
    ]
    dependencies = {}
    for manifest in sorted(manifests):
        for declaration in _declarations(manifest):
            name = declaration.split("#", 1)[0].strip()
            match = _PACKAGE.match(name)
            if match:
                distribution = re.sub(r"[-_.]+", "-", match.group(1)).lower()
                imported = _IMPORT_ALIASES.get(distribution, distribution.replace("-", "_"))
                dependencies.setdefault(imported, TestDependency(
                    distribution=distribution, declared_in=manifest.relative_to(root).as_posix()
                ))
    return dependencies


def _choose_scope(analysis: RepoAnalysisResponse, hints: set[str]) -> PurePosixPath:
    scopes = {
        PurePosixPath(command.working_directory)
        for command in analysis.baseline_test_commands if command.framework in ("pytest", "unittest")
    }
    if not scopes and analysis.test_framework == "pytest" and analysis.test_framework_config_file:
        scopes.add(PurePosixPath(analysis.test_framework_config_file).parent)
    if not scopes:
        return PurePosixPath(".")
    ranked = sorted(
        scopes, key=lambda scope: (
            -sum(PurePosixPath(hint).is_relative_to(scope) for hint in hints), -len(scope.parts), str(scope)
        ),
    )
    if len(ranked) > 1 and not any(PurePosixPath(hint).is_relative_to(ranked[0]) for hint in hints):
        raise TestGenerationInputError("The snapshot has multiple Python projects. Identify a project file in relevant_files.")
    if ranked[0].parts and any(not PurePosixPath(hint).is_relative_to(ranked[0]) for hint in hints):
        raise TestGenerationInputError("Native test generation supports one Python project per task.")
    return ranked[0]


class PythonTestAdapter:
    def inspect(
        self, root: Path, analysis: RepoAnalysisResponse, hints: set[str], preferred: str | None
    ) -> NativeTestProject:
        scope = _choose_scope(analysis, hints)
        directory = root / scope
        dependencies = _python_dependencies(root, scope)
        config, settings = pytest_settings(directory)
        existing = sorted(
            path.relative_to(root).as_posix() for path in directory.rglob("*.py")
            if path.is_file() and not path.is_symlink()
            and not any(part in IGNORED_DIRS for part in path.relative_to(root).parts)
            and not is_protected_path(path.relative_to(root).parts)
        )
        if not existing:
            raise TestGenerationInputError("The selected project has no Python source files.")
        verified_frameworks = {
            command.framework for command in analysis.baseline_test_commands
            if PurePosixPath(command.working_directory) == scope and command.framework in ("pytest", "unittest")
        }
        if config:
            verified_frameworks.add("pytest")
        if "pytest" in dependencies:
            verified_frameworks.add("pytest")
        if not verified_frameworks and analysis.test_framework == "unittest":
            verified_frameworks.add("unittest")
        if not verified_frameworks:
            # A production Python project with no indicated test harness can use the stdlib.
            if any(fnmatch.fnmatchcase(PurePosixPath(path).name, "test*.py") for path in existing):
                raise TestGenerationInputError("The snapshot's Python test framework is not established by its files or manifests.")
            verified_frameworks.add("unittest")
        if preferred and preferred.lower() not in verified_frameworks:
            raise TestGenerationInputError("The requested framework is not established in the selected Python project.")
        framework = preferred.lower() if preferred else "pytest" if "pytest" in verified_frameworks else "unittest"
        if framework == "pytest" and "pytest" not in dependencies:
            raise TestGenerationInputError("The pytest dependency must be declared in the selected project's manifests.")

        file_patterns = (
            setting_words(settings.get("python_files"), ("test_*.py", "*_test.py"))
            if framework == "pytest" else ("test*.py",)
        )
        functions = setting_words(settings.get("python_functions"), ("test",)) if framework == "pytest" else ("test",)
        classes = setting_words(settings.get("python_classes"), ("Test",)) if framework == "pytest" else ()
        configured_dirs = []
        for path in setting_words(settings.get("testpaths")) if framework == "pytest" else ():
            # Only exact repository-relative testpaths are supported.
            if path == ".":
                configured_dirs.append(scope)
                continue
            try:
                explicit = solution_path_parts(path.rstrip("/"))
            except ValueError as exc:
                raise TestGenerationInputError("Unsupported pytest testpaths setting.") from exc
            configured_dirs.append(scope.joinpath(*explicit))
        originals = [
            path for path in existing
            if any(fnmatch.fnmatchcase(PurePosixPath(path).name, pattern) for pattern in file_patterns)
            and (not configured_dirs or any(PurePosixPath(path).is_relative_to(item) for item in configured_dirs))
        ]
        directories = {PurePosixPath(path).parent.as_posix() for path in originals}
        if not directories:
            directories = {path.as_posix() for path in configured_dirs} or {(scope / "tests").as_posix()}
        production = {
            path for path in existing if path not in originals
            and not any(part in TEST_DIRECTORIES for part in PurePosixPath(path).parts[:-1])
            and PurePosixPath(path).name != "conftest.py"
        }
        return NativeTestProject(
            scope, framework, (scope / config).as_posix() if config and framework == "pytest" else None,
            tuple(sorted(directories)), file_patterns, functions, classes, dependencies,
            python_module_paths(production, scope),
            set(python_module_paths(set(existing) - production, scope)),
            [OriginalRegressionFile(path=path, sha256=hashlib.sha256((root / path).read_bytes()).hexdigest())
             for path in originals],
        )

    def commands(self, project: NativeTestProject, paths: list[str]) -> list[BaselineTestCommand]:
        relative = [PurePosixPath(path).relative_to(project.scope) for path in paths]
        if not relative:
            return []
        cwd = project.scope.as_posix()
        if project.framework == "pytest":
            return [BaselineTestCommand(framework="pytest", working_directory=cwd,
                                        argv=["python", "-m", "pytest", "-q", "--", *map(str, relative)])]
        return [
            BaselineTestCommand(framework="unittest", working_directory=cwd, argv=[
                "python", "-m", "unittest", "discover", "-s", path.parent.as_posix(), "-p", path.name,
            ])
            for path in relative
        ]

    def validate_file(self, root: Path, project: NativeTestProject, *, path: str, code: str,
                      new_modules: dict[str, str]) -> TestInspection:
        from app.services.python_test_quality import inspect_python_test

        return inspect_python_test(root, project, path=path, code=code, new_modules=new_modules)


def get_test_adapter(language: str) -> TestFrameworkAdapter:
    # Future adapters implement the same inspection, file validation and argv contract.
    if language.strip().lower() != "python":
        raise TestGenerationInputError("Native test generation currently supports Python snapshots only.")
    return PythonTestAdapter()
