"""Trusted entry point copied into a sandbox bundle and run only in the container."""

import compileall
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import unittest

BUNDLE = Path("/bundle")
REPO = BUNDLE / "repo"
SITE = "/work/site"
BYTECODE = "/work/bytecode"
MAX_LOG = 32 * 1024
MAX_CASES = 512
INSTALL_ARGV = [
    "/usr/local/bin/python", "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
    "--no-index", "--find-links=/bundle/wheels", "--require-hashes", "--only-binary=:all:",
    "--no-deps", "--no-compile", "--no-cache-dir", "--target", SITE, "-r", "/bundle/requirements.lock",
]


class LimitedLog(io.TextIOBase):
    def __init__(self):
        self.chunks = []
        self.remaining = MAX_LOG
        self.truncated = False

    def write(self, text):
        chunk = text[:self.remaining]
        if chunk:
            self.chunks.append(chunk)
            self.remaining -= len(chunk)
        if len(text) > len(chunk):
            self.truncated = True
        return len(text)

    def flush(self):
        pass

    def value(self):
        return "".join(self.chunks)


def install(log, commands):
    lock = BUNDLE / "requirements.lock"
    if not lock.is_file():
        return True
    started = time.monotonic()
    with subprocess.Popen(INSTALL_ARGV, cwd="/work", stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        while chunk := process.stdout.read(4096):
            log.write(chunk.decode("utf-8", "replace"))
        code = process.wait()
    commands.append({"argv": INSTALL_ARGV, "working_directory": "/work", "exit_code": code,
                     "duration_ms": round((time.monotonic() - started) * 1000)})
    return code == 0


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run_unittest(targets, selected, log):
    rows = []
    category = None
    selected = {(item["path"], item["selector"]) for item in selected}
    for target in targets:
        path = REPO / target
        loader = unittest.TestLoader()
        suite = loader.discover(str(path.parent), pattern=path.name)
        if loader.errors:
            log.write("\n".join(loader.errors))
            return 2, "collection_error", rows

        def selector(test):
            return test.id().removeprefix(test.__class__.__module__ + ".")

        tests = [test for test in flatten(suite) if not selected or (target, selector(test)) in selected]
        if len(rows) + len(tests) > MAX_CASES:
            return 2, "resource_failure", rows
        if not tests:
            return 5, "no_tests", rows

        class Observer(unittest.TestResult):
            def __init__(self):
                super().__init__()
                self.records = {}
                self.started = {}

            def startTest(self, test):
                super().startTest(test)
                self.started[test] = time.monotonic()
                self.record(test, "error")

            def record(self, test, outcome, error=None):
                now = time.monotonic()
                prior = self.records.get(test)
                if prior and prior["outcome"] in {"error", "assertion_failed"} and outcome == "skipped":
                    return
                self.records[test] = {
                    "path": target, "selector": selector(test), "native_id": test.id(), "outcome": outcome,
                    "duration_ms": round((now - self.started.get(test, now)) * 1000),
                }
                if error:
                    log.write("".join(traceback.format_exception(*error)))

            def addSuccess(self, test):
                self.record(test, "passed")

            def addError(self, test, err):
                self.record(test, "error", err)

            def addFailure(self, test, err):
                self.record(test, "assertion_failed", err)

            def addSkip(self, test, reason):
                self.record(test, "skipped")

            def addExpectedFailure(self, test, err):
                self.record(test, "skipped")

            def addUnexpectedSuccess(self, test):
                self.record(test, "skipped")

            def addSubTest(self, test, subtest, err):
                if err:
                    self.record(test, "assertion_failed" if issubclass(err[0], AssertionError) else "error", err)

        observer = Observer()
        unittest.TestSuite(tests).run(observer)
        rows.extend(observer.records.values())
        if len(observer.records) != len(tests):
            category = "test_error"
    return test_status(rows, category)


def test_status(rows, category=None):
    outcomes = {row["outcome"] for row in rows}
    if category:
        return 2, category, rows
    if not rows:
        return 5, "no_tests", rows
    if "error" in outcomes:
        return 1, "test_error", rows
    if "skipped" in outcomes:
        return 1, "test_skipped", rows
    if "assertion_failed" in outcomes:
        return 1, "assertion_failure", rows
    return 0, None, rows


def run_pytest(pytest, targets, selected, config=None):
    records = {}
    collection_error = False
    too_many = False
    selected = {(item["path"], item["selector"]) for item in selected}

    def key(item):
        path = item.path.relative_to(REPO).as_posix()
        leaf = item.nodeid.split("::", 1)[1].split("[", 1)[0].replace("::", ".")
        return path, leaf

    class Observer:
        def pytest_collection_modifyitems(self, config, items):
            nonlocal too_many
            if selected:
                items[:] = [item for item in items if key(item) in selected]
            if len(items) > MAX_CASES:
                too_many = True
                items.clear()

        def pytest_collectreport(self, report):
            nonlocal collection_error
            if report.failed or report.skipped:
                collection_error = True

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            path, selector = key(item)
            record = records.setdefault(item.nodeid, {
                "path": path, "selector": selector, "native_id": item.nodeid, "outcome": "error", "duration_ms": 0,
            })
            record["duration_ms"] += round(report.duration * 1000)
            if report.skipped or hasattr(report, "wasxfail"):
                record["outcome"] = "skipped"
            elif report.failed:
                exception = call.excinfo
                record["outcome"] = "assertion_failed" if (
                    report.when == "call" and exception and exception.errisinstance(AssertionError)
                ) else "error"
            elif report.when == "call":
                record["outcome"] = "passed"

    config_args = [] if config is None else ["-c", str(REPO / config) if config else "/dev/null"]
    code = int(pytest.main(["-q", "-p", "no:cacheprovider", *config_args, "--", *(str(REPO / path) for path in targets)],
                           plugins=[Observer()]))
    rows = list(records.values())
    if too_many:
        return 2, "resource_failure", rows
    if collection_error or code in (2, 3, 4):
        return code or 2, "collection_error", rows
    classified, category, rows = test_status(rows)
    return code or classified, category if classified or not code else "test_error", rows


def run(plan, stdout, stderr, commands):
    sys.dont_write_bytecode = True
    if not install(stderr, commands):
        return 1, "dependency_installation", []
    if plan["kind"] == "compile":
        sys.pycache_prefix = BYTECODE
        success = compileall.compile_dir(str(REPO / plan["cwd"]), quiet=1, workers=1)
        return (0, None, []) if success else (1, "build_failed", [])

    sys.path.insert(0, SITE)
    pytest = None
    if plan["framework"] == "pytest":
        import pytest
    cwd = REPO / plan["cwd"]
    os.chdir(cwd)
    sys.path[:0] = [str(cwd), str(cwd / "src")]
    if pytest is not None:
        return run_pytest(pytest, plan["targets"], plan["selectors"], plan.get("config"))
    return run_unittest(plan["targets"], plan["selectors"], stdout)


def main():
    os.environ.clear()
    os.environ.update(PATH="/usr/local/bin:/usr/bin:/bin", HOME="/work", TMPDIR="/tmp",
                      LANG="C.UTF-8", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PIP_CONFIG_FILE="/dev/null")
    plan = json.loads((BUNDLE / "plan.json").read_bytes())
    return emit_result(plan)


def emit_result(plan):
    stdout, stderr, commands = LimitedLog(), LimitedLog(), []
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code, category, cases = run(plan, stdout, stderr, commands)
        except OSError as exc:
            traceback.print_exc()
            code, category, cases = 2, "resource_failure" if exc.errno in (12, 24, 27, 28) else "test_error", []
        except MemoryError:
            code, category, cases = 2, "resource_failure", []
        except BaseException:
            traceback.print_exc()
            code, category, cases = 2, "test_error", []
    print(json.dumps({"exit_code": code, "failure_category": category, "cases": cases, "commands": commands,
                      "stdout": stdout.value(), "stderr": stderr.value(),
                      "output_truncated": stdout.truncated or stderr.truncated}, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
