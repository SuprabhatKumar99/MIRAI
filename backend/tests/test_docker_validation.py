"""Exercise the validation pipeline with a fake backend and the local Docker engine."""

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.schemas.validation import CaseOutcome, CommandEvidence, SandboxRun, ValidationRequest
from app.services.docker_sandbox import (
    DockerExecutionBackend, SandboxError, SandboxPlan, ValidationExecutionBackend, _flags, write_bundle,
)
from app.services.isolated_validation import require_recorded_validation
from app.services.sandbox_dependencies import (
    DependencyBundle, DependencyPolicyError, configured_bundle, verify_project_dependencies,
)
from app.services.validation_service import validate_task_package
from tests.repository_fixtures import SnapshotTestCase
from tests.test_native_test_generation import feature_case
from tests.test_repository_snapshots import client, upload, zip_bytes
from tests.test_validation_endpoint import SAMPLE_TASK, SAMPLE_SOLUTION

BASE = "def reserve():\n    return 1\n\ndef stable():\n    return 5\n"
FIXED = BASE.replace("return 1", "return 2")
ORIGINAL = (
    "import unittest\nfrom warehouse import stable\n"
    "class Original(unittest.TestCase):\n"
    "    def test_existing(self):\n        self.assertEqual(stable(), 5)\n"
)
FEATURE = (
    "from unittest import TestCase\nfrom warehouse import reserve\n"
    "class Feature(TestCase):\n"
    "    def test_reservation(self):\n        self.assertEqual(reserve(), 2)\n"
)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ValidationFixture(SnapshotTestCase):
    def setUp(self):
        super().setUp()
        self.reports = self.snapshot_directory.parent / "reports"
        env = patch.dict(os.environ, {"CTF_VALIDATION_REPORT_DIR": str(self.reports), "CTF_VALIDATION_WHEELHOUSE": ""})
        env.start()
        self.addCleanup(env.stop)

    def package(self, *, source=FIXED, feature=FEATURE, original=ORIGINAL, files=None,
                strategy="untreated_baseline"):
        uploaded = upload(zip_bytes({
            "repository/warehouse.py": BASE, "repository/tests/test_original.py": original,
            **(files or {}),
        }))
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        snapshot = uploaded.json()["snapshot_id"]
        task = {**SAMPLE_TASK, "snapshot_id": snapshot, "starting_commit_sha": None,
                "description": "Update the number of reserved units.", "relevant_files": ["warehouse.py"],
                "requirements": ["Reserve two units"], "acceptance_criteria": ["The reservation returns two units"]}
        solution = {**SAMPLE_SOLUTION, "snapshot_id": snapshot, "files": [{
            "path": "warehouse.py", "status": "modify", "language": "python", "code": source,
            "explanation": "Update the reservation result.", "base_sha256": digest(BASE),
            "result_sha256": digest(source), "acceptance_criteria_ids": ["AC1"],
        }]}
        candidate = {"feature_test_files": [{
            "path": "tests/test_feature.py", "framework": "unittest", "code": feature,
            "test_cases": [feature_case("Feature.test_reservation", "R1", "AC1", strategy=strategy)],
        }], "regression_test_files": []}
        with patch("app.services.test_service._get_gemini_client") as provider:
            provider.return_value.models.generate_content.return_value = SimpleNamespace(text=json.dumps(candidate))
            generated = client.post("/tasks/tests", json={"task": task})
        self.assertEqual(generated.status_code, 201, generated.text)
        return {"task": task, "solution": solution, "test_suite": generated.json(), "execution_mode": "docker"}

    def validate(self, payload, backend=None):
        return validate_task_package(None, ValidationRequest.model_validate(payload), backend)


class ScriptedBackend(ValidationExecutionBackend):
    image_id = "sha256:" + "a" * 64

    def __init__(self, *, failure=None, change_collection=False):
        self.failure = failure
        self.change_collection = change_collection
        self.workspaces = []

    def probe(self):
        if self.failure == "infrastructure_unavailable":
            raise SandboxError("infrastructure_unavailable", "Docker is unavailable in this fixture.")

    def run(self, workspace, plan, dependencies, log_path):
        self.workspaces.append(((workspace / "warehouse.py").read_text(), plan))
        number = len(self.workspaces)
        category = {1: None, 2: "build_failed", 3: "assertion_failure", 4: "test_error"}.get(number)
        exit_code = 0
        outcome = "passed"
        if self.failure == (number, "timeout"):
            return SandboxRun(timeout_seconds=1, timed_out=True, failure_category="timeout")
        if self.failure == (number, "resource_failure"):
            return SandboxRun(timeout_seconds=1, exit_code=137, oom_killed=True, failure_category="resource_failure")
        if self.failure == number and number < 5:
            exit_code, outcome = 1, "error"
        elif number <= 4 or self.failure == "escaped":
            category = None
        else:
            exit_code, outcome, category = 1, "assertion_failed", "assertion_failure"
        selectors = plan.selectors or tuple(
            (path, "Original.test_other" if number == 4 and self.change_collection else "Original.test_existing")
            for path in plan.targets
        )
        log_path.write_text("fixture log\n")
        return SandboxRun(
            timeout_seconds=1, exit_code=exit_code, failure_category=category,
            log_location=f"{log_path.parent.name}/{log_path.name}",
            commands=[CommandEvidence(argv=["fixture runner", plan.kind], working_directory=plan.cwd, exit_code=exit_code)],
            cases=[CaseOutcome(path=path, selector=name, outcome=outcome) for path, name in selectors],
        )


class TestValidationGates(ValidationFixture):
    def test_five_gates_on_clean_verified_sources(self):
        payload = self.package()
        payload["test_suite"]["execution_plan"]["feature"][0]["argv"] = ["untrusted-command"]
        backend = ScriptedBackend()
        report = self.validate(payload, backend)
        self.assertEqual(report.overall_verdict, "VALIDATED", report.model_dump_json())
        self.assertEqual([gate.status for gate in report.gates], ["passed"] * 5)
        self.assertEqual([source for source, _ in backend.workspaces], [BASE, FIXED, FIXED, FIXED, BASE])
        self.assertEqual(report.gates[-1].runs[0].exit_code, 1)
        self.assertTrue((self.reports / report.evidence_location).is_file())
        self.assertTrue((self.reports / report.gates[-1].runs[0].log_location).is_file())
        package = ValidationRequest.model_validate(payload)
        require_recorded_validation(report, package.task, package.solution, package.test_suite)
        package.task.description = "Changed after validation"
        with self.assertRaisesRegex(ValueError, "server-recorded"):
            require_recorded_validation(report, package.task, package.solution, package.test_suite)

    def test_failures_prevent_validated_verdict(self):
        payload = self.package()
        for failure, gate, category, verdict in (
            (2, 1, "build_failed", "REJECTED"), (3, 2, "assertion_failure", "REJECTED"),
            (4, 3, "test_error", "REJECTED"), ("escaped", 4, "negative_control_escaped", "REJECTED"),
            ((3, "timeout"), 2, "timeout", "NOT_VALIDATED"),
            ((1, "resource_failure"), 0, "resource_failure", "NOT_VALIDATED"),
        ):
            with self.subTest(failure=failure):
                report = self.validate(payload, ScriptedBackend(failure=failure))
                self.assertEqual(report.overall_verdict, verdict)
                self.assertEqual(report.failure_category, category)
                self.assertEqual(report.gates[gate].status, "failed")
                self.assertTrue(all(item.status == "not_run" for item in report.gates[gate+1:]))

    def test_missing_docker_returns_truthful_api_result(self):
        payload = self.package()
        with patch.dict(os.environ, {"CTF_VALIDATION_IMAGE": ""}):
            response = client.post("/tasks/validate", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["overall_verdict"], "NOT_VALIDATED")
        self.assertEqual(response.json()["failure_category"], "infrastructure_unavailable")
        self.assertEqual(self.validate(payload, ScriptedBackend(failure="infrastructure_unavailable")).failure_category,
                         "infrastructure_unavailable")

    def test_rejects_changes_to_original_regression_or_collected_cases(self):
        payload = self.package()
        tampered = deepcopy(payload)
        tampered["test_suite"]["original_regression_files"][0]["sha256"] = digest("claim")
        self.assertEqual(self.validate(tampered, ScriptedBackend()).failure_category, "artifact_integrity")
        self.assertEqual(self.validate(payload, ScriptedBackend(change_collection=True)).failure_category, "test_error")

    def test_patch_conflict_and_security_protection(self):
        payload = self.package()
        payload["solution"]["files"][0]["base_sha256"] = digest("wrong base")
        report = self.validate(payload, ScriptedBackend())
        self.assertEqual(report.failure_category, "patch_conflict")
        self.assertEqual(report.gates[0].status, "passed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("PRIVATE_TEST_VALUE=fixture")
            with self.assertRaises(SandboxError) as caught:
                write_bundle(io.BytesIO(), root, SandboxPlan("compile", ".", "unittest"), DependencyBundle())
            self.assertEqual(caught.exception.category, "security_policy")


class TestSandboxPolicy(unittest.TestCase):
    def test_explicit_test_groups_take_precedence_over_general_dev_tools(self):
        bundle = DependencyBundle(pins=(("pytest", "8.3.5"), ("freezegun", "1.5.2")))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "pyproject.toml"
            manifest.write_text(
                '[dependency-groups]\ntests = ["pytest", "freezegun"]\ndev = ["ruff"]\n'
            )
            verify_project_dependencies(root, "pytest", bundle)
            with self.assertRaises(DependencyPolicyError):
                verify_project_dependencies(root, "pytest", DependencyBundle(pins=(("pytest", "8.3.5"),)))
            manifest.write_text('[dependency-groups]\ndev = ["ruff"]\n')
            with self.assertRaises(DependencyPolicyError):
                verify_project_dependencies(root, "pytest", bundle)

    def test_only_operator_pins_are_accepted_and_rehashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            wheel = wheelhouse / "some_module-1.0-py3-none-any.whl"
            wheel.write_bytes(b"fixture wheel content")
            (wheelhouse / "requirements.lock").write_text(
                f"some-module==1.0 --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n"
            )
            bundle = configured_bundle(str(wheelhouse.resolve()))
            manifest = root / "requirements.txt"
            manifest.write_text("some-module>=1.0\n")
            verify_project_dependencies(root, "unittest", bundle)
            for forbidden in ("--extra-index-url https://example.invalid", "some-module @ file:///host.whl",
                              "../host.whl", "some-module>=2.0"):
                with self.subTest(value=forbidden):
                    manifest.write_text(forbidden)
                    with self.assertRaises(DependencyPolicyError):
                        verify_project_dependencies(root, "unittest", bundle)
            wheel.write_bytes(b"altered contents")
            with self.assertRaises(DependencyPolicyError):
                configured_bundle(str(wheelhouse.resolve()))

    def test_workload_cli_uses_fixed_nonprivileged_limits(self):
        flags = _flags("ctf-fixture", "65534:65534", "/usr/bin/timeout")
        for option, expected in {
            "--network": "none", "--ipc": "none", "--user": "65534:65534", "--cap-drop": "ALL",
            "--security-opt": "no-new-privileges:true", "--pull": "never", "--cpus": "1", "--pids-limit": "64",
            "--memory": "512m", "--memory-swap": "512m",
        }.items():
            self.assertEqual(flags[flags.index(option) + 1], expected)
        self.assertIn("--read-only", flags)
        self.assertTrue({"--privileged", "--env-file", "--volume", "--publish"}.isdisjoint(flags))


@unittest.skipUnless(os.getenv("CTF_TEST_DOCKER_IMAGE"), "Set CTF_TEST_DOCKER_IMAGE to the approved local image ID")
class TestDockerGates(ValidationFixture):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"CTF_VALIDATION_IMAGE": os.environ["CTF_TEST_DOCKER_IMAGE"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_docker_api_validates_and_detects_missing_feature(self):
        original = ORIGINAL.replace(
            "        self.assertEqual(stable(), 5)",
            "        import os\n"
            "        from pathlib import Path\n"
            "        self.assertEqual(os.getuid(), 65534)\n"
            "        self.assertFalse(Path('/var/run/docker.sock').exists())\n"
            "        self.assertIn('NoNewPrivs:\\t1', Path('/proc/self/status').read_text())\n"
            "        with self.assertRaises(OSError):\n"
            "            Path('/bundle/repo/warehouse.py').write_text('changed')\n"
            "        self.assertEqual(stable(), 5)",
        )
        response = client.post("/tasks/validate", json=self.package(original=original))
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["overall_verdict"], "VALIDATED", self.logs(report))
        self.assertEqual([gate["runs"][0]["exit_code"] for gate in report["gates"]], [0, 0, 0, 0, 1])
        self.assertEqual(report["gates"][-1]["runs"][0]["cases"][0]["outcome"], "assertion_failed")
        self.assertEqual(report["sandbox_image_id"], os.environ["CTF_TEST_DOCKER_IMAGE"])
        self.assertTrue((self.reports / report["evidence_location"]).is_file())

    def logs(self, report):
        logs = []
        for gate in report["gates"]:
            for run in gate["runs"]:
                if run["log_location"]:
                    logs.append((self.reports / run["log_location"]).read_text(errors="replace"))
        return json.dumps(report) + "\n" + "\n".join(logs)

    def test_build_failure_and_escaped_negative_control(self):
        broken = self.validate(self.package(source="def incomplete(\n"))
        self.assertEqual(broken.failure_category, "build_failed", self.logs(broken.model_dump()))
        weak = self.package(source=FIXED.replace("return 2", "return 1"),
                            feature=FEATURE.replace("reserve(), 2", "reserve(), 1"))
        escaped = self.validate(weak)
        self.assertEqual(escaped.failure_category, "negative_control_escaped", self.logs(escaped.model_dump()))
        self.assertEqual(escaped.gates[-1].runs[0].exit_code, 0)

    def test_wrong_return_mutation_and_real_regression_failure(self):
        result = self.validate(self.package(strategy="mutated_reference"))
        self.assertEqual(result.overall_verdict, "VALIDATED", self.logs(result.model_dump()))
        self.assertEqual(result.gates[-1].runs[0].variant, "wrong_return:warehouse.py:2")
        result = self.validate(self.package(source=FIXED.replace("return 5", "return 6")))
        self.assertEqual(result.failure_category, "assertion_failure", self.logs(result.model_dump()))
        self.assertEqual(result.gates[3].status, "failed")

    def test_real_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "test_wait.py").write_text(
                "import time, unittest\nclass Wait(unittest.TestCase):\n"
                "    def test_wait(self):\n        time.sleep(60)\n"
            )
            backend = DockerExecutionBackend(os.environ["CTF_TEST_DOCKER_IMAGE"], "timeout-fixture", root, timeout=3)
            backend.probe()
            result = backend.run(workspace, SandboxPlan("tests", ".", "unittest", ("test_wait.py",)),
                                 DependencyBundle(), root / "timeout.log")
            self.assertTrue(result.timed_out, (root / "timeout.log").read_text())
            self.assertEqual(result.failure_category, "timeout")
            self.assertTrue(result.commands)
