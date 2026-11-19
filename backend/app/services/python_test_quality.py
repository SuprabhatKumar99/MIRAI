"""Non-executing checks for collectible, behavior-observing native Python tests."""

import ast
import fnmatch
import re
import sys
from pathlib import Path, PurePosixPath

from app.services.solution_changes import has_secret_marker, resolve_safe_workspace_path
from app.schemas.test_suite import TestDependency
from app.services.test_adapter import NativeTestProject, TestInspection

MAX_TEST_BYTES = 24 * 1024
_PLACEHOLDER = re.compile(r"#\s*(?:todo\b|fixme\b|implement here|write tests here|placeholder)", re.I)
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "load_tests", "pytest_plugins", "pytestmark",
    "__test__", "skip", "skipif", "skipunless", "skiptest", "xfail",
    "expectedfailure", "exit", "_exit", "system", "systemexit",
}
_ASSERTIONS = {
    "assertEqual", "assertNotEqual", "assertTrue", "assertFalse", "assertIs", "assertIsNot",
    "assertIsNone", "assertIsNotNone", "assertIn", "assertNotIn", "assertIsInstance",
    "assertNotIsInstance", "assertGreater", "assertGreaterEqual", "assertLess", "assertLessEqual",
    "assertAlmostEqual", "assertNotAlmostEqual", "assertRegex", "assertNotRegex", "assertCountEqual",
    "assertSequenceEqual", "assertDictEqual", "assertListEqual", "assertSetEqual", "assertTupleEqual",
}
_RAISES = {"raises", "assertRaises", "assertRaisesRegex"}
_WIDE_EXCEPTIONS = {"Exception", "BaseException", "AssertionError", "pytest.fail.Exception"}
_MOCK_FACTORIES = {"Mock", "MagicMock", "AsyncMock", "patch", "create_autospec"}
_RUNNER_OVERRIDES = {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}


class TestQualityError(ValueError):
    """Static evidence is insufficient for a candidate generated test."""


def _qualname(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualname(node.value)
        return f"{parent}.{node.attr}" if parent else ""
    return ""


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(name, pattern) if any(symbol in pattern for symbol in "*?[")
        else name.startswith(pattern)
        for pattern in patterns
    )


def _collect_cases(tree: ast.Module, project: NativeTestProject) -> dict[str, tuple[ast.FunctionDef, bool]]:
    result = {}
    unittest_bases = {"unittest.TestCase"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "unittest":
            unittest_bases.update(alias.asname or alias.name for alias in node.names if alias.name == "TestCase")
        if isinstance(node, ast.Import):
            unittest_bases.update(f"{alias.asname}.TestCase" for alias in node.names
                                  if alias.name == "unittest" and alias.asname)

    def add_case(name: str, case: ast.FunctionDef | ast.AsyncFunctionDef, unittest_case: bool = False) -> None:
        if name in result:
            raise TestQualityError("A Python file defines the same test selector twice.")
        if isinstance(case, ast.AsyncFunctionDef):
            raise TestQualityError("Generated cases require synchronous entry points for native collection.")
        result[name] = (case, unittest_case)

    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, functions) and project.framework == "pytest" and _matches(node.name, project.function_patterns):
            add_case(node.name, node)
        if not isinstance(node, ast.ClassDef):
            continue
        is_testcase = any(_qualname(base) in unittest_bases for base in node.bases)
        is_pytest_class = project.framework == "pytest" and _matches(node.name, project.class_patterns)
        if not is_testcase and not is_pytest_class:
            continue
        if any(isinstance(child, functions) and child.name in ("__init__", "__new__") for child in node.body):
            raise TestQualityError("Generated test classes must remain collectible.")
        patterns = ("test",) if is_testcase else project.function_patterns
        for child in node.body:
            if isinstance(child, functions) and _matches(child.name, patterns):
                add_case(f"{node.name}.{child.name}", child, is_testcase)
    return result


def _imports(
    tree: ast.Module, path: PurePosixPath, project: NativeTestProject, new_modules: dict[str, str]
) -> tuple[dict[ast.stmt, dict[str, set[str]]], list[TestDependency], set[str]]:
    repository = {**project.modules, **new_modules}
    namespace = path.parent.relative_to(project.scope).parts
    bindings: dict[ast.stmt, dict[str, set[str]]] = {}
    dependencies = {"pytest": project.dependencies["pytest"]} if project.framework == "pytest" else {}
    new_imports = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        prefix = ""
        if isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(namespace) + 1:
                    raise TestQualityError("Relative import escapes the selected test project.")
                base = namespace[:len(namespace) - node.level + 1]
                prefix = ".".join((*base, node.module or "")).strip(".")
            else:
                prefix = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                raise TestQualityError("Generated tests must use explicit imports.")
            module = f"{prefix}.{alias.name}" if prefix else alias.name
            top = module.split(".", 1)[0]
            if top in ("subprocess", "importlib", "_pytest"):
                raise TestQualityError("Dynamic loading, shell execution, and runner internals are forbidden.")
            source = repository.get(module) or repository.get(prefix)
            if source:
                bound = alias.asname or (alias.name.split(".", 1)[0] if isinstance(node, ast.Import) else alias.name)
                bindings.setdefault(node, {}).setdefault(bound, set()).add(source)
                if source in new_modules.values():
                    new_imports.add(source)
            elif top in sys.stdlib_module_names or module in project.test_helper_modules or prefix in project.test_helper_modules:
                continue
            elif top in project.dependencies and not (top == "pytest" and project.framework == "unittest"):
                dependencies[top] = project.dependencies[top]
            else:
                raise TestQualityError("Generated tests import an unsupported dependency or unknown repository module.")
    return bindings, [item for _, item in sorted(dependencies.items())], new_imports


def _observed(expression: ast.AST, bindings: dict[str, set[str]]) -> set[str]:
    return set().union(*(bindings.get(item.id, set()) for item in ast.walk(expression) if isinstance(item, ast.Name)))


def _constant_only(expression: ast.AST) -> bool:
    return not any(isinstance(item, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)) for item in ast.walk(expression))


def check_assertion_predicate(expression: ast.AST) -> None:
    nodes = list(ast.walk(expression))
    if _constant_only(expression) or any(
        isinstance(item, ast.BoolOp) and any(_constant_only(part) for part in item.values)
        or isinstance(item, ast.IfExp) and _constant_only(item.body) and _constant_only(item.orelse)
        or isinstance(item, ast.Compare) and any(
            ast.dump(left) == ast.dump(right)
            for left, right in zip([item.left, *item.comparators], item.comparators)
        )
        for item in nodes
    ):
        raise TestQualityError("Generated tests contain an unconditional or placeholder assertion.")


def _case_evidence(
    case: ast.FunctionDef, globals_: dict[str, set[str]],
    imports: dict[ast.stmt, dict[str, set[str]]], unittest_case: bool,
) -> set[str]:
    """Conservative path evidence from imported code through simple Python assignments."""
    bindings = dict(globals_)
    observed = set()
    # Use textual statement order for bindings. Literal dead branches / nested functions
    # cannot supply a qualifying assertion.
    def visit_block(body: list[ast.stmt]) -> None:
        for statement in body:
            if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                break
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if statement in imports:
                bindings.update(imports[statement])
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                mock_factory = isinstance(statement.value, ast.Call) and (
                    _qualname(statement.value.func).rsplit(".", 1)[-1] in _MOCK_FACTORIES
                )
                paths = set() if mock_factory else _observed(statement.value, bindings)
                for target in targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = paths
            if isinstance(statement, ast.Assert):
                check_assertion_predicate(statement.test)
                observed.update(_observed(statement.test, bindings))
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                owner, _, method = _qualname(call.func).rpartition(".")
                if unittest_case and owner == "self" and method in _ASSERTIONS and call.args:
                    operands = call.args[:1] if method in ("assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone") else call.args[:2]
                    if all(_constant_only(value) for value in operands) or (
                        len(operands) == 2 and ast.dump(operands[0]) == ast.dump(operands[1])
                    ):
                        raise TestQualityError("Generated tests contain an unconditional or placeholder assertion.")
                    if len(operands) == 1:
                        check_assertion_predicate(operands[0])
                    for value in operands:
                        observed.update(_observed(value, bindings))
                if unittest_case and owner == "self" and method in _RAISES and len(call.args) > (2 if method == "assertRaisesRegex" else 1):
                    if _qualname(call.args[0]) in _WIDE_EXCEPTIONS:
                        raise TestQualityError("Exception tests must name a specific expected error.")
                    observed.update(_observed(call.args[2 if method == "assertRaisesRegex" else 1], bindings))
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    expr = item.context_expr
                    if isinstance(item.optional_vars, ast.Name):
                        bindings[item.optional_vars.id] = _observed(expr, bindings)
                    call_name = _qualname(expr.func) if isinstance(expr, ast.Call) else ""
                    exception_check = call_name == "pytest.raises" or (
                        unittest_case and call_name in ("self.assertRaises", "self.assertRaisesRegex")
                    )
                    if exception_check:
                        if not expr.args or _qualname(expr.args[0]) in _WIDE_EXCEPTIONS:
                            raise TestQualityError("Exception tests must name a specific expected error.")
                        for child in statement.body:
                            if isinstance(child, (ast.Expr, ast.Assign, ast.AnnAssign)) and isinstance(child.value, ast.Call):
                                observed.update(_observed(child.value, bindings))
                visit_block(statement.body)
            elif isinstance(statement, ast.If) and isinstance(statement.test, ast.Constant):
                visit_block(statement.body if statement.test.value else statement.orelse)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                visit_block(statement.body)
                visit_block(statement.finalbody)
            # Conditional/loop-only assertions can be skipped. Require a straight-line check.

    visit_block(case.body)
    if not observed:
        raise TestQualityError("Every collected test must assert an observable result from repository code.")
    return observed


def inspect_python_test(
    root: Path, project: NativeTestProject, *, path: str, code: str, new_modules: dict[str, str]
) -> TestInspection:
    target = resolve_safe_workspace_path(root, path)
    relative = PurePosixPath(path)
    if not all(part not in ("", ".", "..") for part in relative.parts) or target.exists():
        raise TestQualityError("Generated tests must use new repository-relative paths.")
    if relative.parent.as_posix() not in project.directories or not any(
        fnmatch.fnmatchcase(relative.name, pattern) for pattern in project.file_patterns
    ) or relative.suffix != ".py" or not relative.stem.isidentifier():
        raise TestQualityError("A generated test path does not match the project's native test layout.")
    try:
        data = code.encode("utf-8")
    except UnicodeError as exc:
        raise TestQualityError("Invalid Unicode in generated test code.") from exc
    if not code.strip() or len(data) > MAX_TEST_BYTES or has_secret_marker(code) or _PLACEHOLDER.search(code):
        raise TestQualityError("Generated tests contain empty, oversized, credential-like or placeholder content.")
    try:
        tree = ast.parse(data, filename="<generated-test>")
        compile(tree, "<generated-test>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, OverflowError, RecursionError) as exc:
        raise TestQualityError("Generated test code fails Python syntax checks.") from exc
    for node in ast.walk(tree):
        name = _qualname(node).rsplit(".", 1)[-1] if isinstance(node, (ast.Attribute, ast.Name)) else ""
        if isinstance(node, ast.Assert):
            check_assertion_predicate(node.test)
        if (
            name.casefold() in _FORBIDDEN_NAMES
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (node.name == "load_tests" or node.name.startswith("pytest_")))
            or (isinstance(node, ast.Constant) and (node.value is Ellipsis or (
                isinstance(node.value, str) and (
                    node.value in _RUNNER_OVERRIDES or "/logs/verifier" in node.value.replace("\\", "/")
                )
            )))
            or (isinstance(node, ast.ExceptHandler) and (
                node.type is None or any(_qualname(item) in _WIDE_EXCEPTIONS for item in ast.walk(node.type))
            ))
        ):
            raise TestQualityError("Generated tests contain runner overrides, skipped checks or forbidden shortcuts.")

    bindings, dependencies, new_imports = _imports(tree, relative, project, new_modules)
    globals_ = {name: paths for node in tree.body for name, paths in bindings.get(node, {}).items()}
    for node in tree.body:
        declared = {item.id for item in ast.walk(node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)}
        if declared & globals_.keys() or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in globals_
        ):
            raise TestQualityError("Generated module code shadows imported repository interfaces.")
    cases = _collect_cases(tree, project)
    if not cases:
        raise TestQualityError("No collectible native Python tests were found.")
    return TestInspection(
        cases={name: _case_evidence(case, globals_, bindings, unittest_case)
               for name, (case, unittest_case) in cases.items()},
        dependencies=dependencies,
        new_imports=new_imports,
    )
