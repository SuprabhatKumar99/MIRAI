"""Evaluation gating, result ingestion, and reporting without paid model calls."""

from collections import Counter
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.evaluation import AgentConfiguration, EvaluationRequest, TestCounts, TrialEvidence
from app.services import evaluation_service
from app.services.evaluation_metrics import summarize
from app.services.harbor_evaluation import HarborEvaluationBackend, parse_trial
from tests.test_docker_validation import ValidationFixture

CONFIG = {"label": "small", "model": "openai/example-snapshot", "max_turns": 3}
CHECKSUM = "a" * 64
TRIAL = "12345678-1234-1234-1234-123456789012"


class FakeBackend:
    origin = "test_double"

    def __init__(self, *, oracle_failed=False, weak_control=False, alter_package=False):
        self.oracle_failed = oracle_failed
        self.weak_control = weak_control
        self.alter_package = alter_package
        self.runs = []

    def probe(self, root, *, live):
        return "fake", "fake"

    def run(self, task_directory, root, trial, plan, originals):
        self.runs.append((trial.role, trial.label, trial.attempt, task_directory))
        trial.harbor_task_checksum = CHECKSUM
        trial.trustworthy = True
        trial.reward = int(not (trial.role == "oracle" and self.oracle_failed) and (
            trial.role != "control" or self.weak_control
        ))
        trial.build_passed = True
        trial.feature_tests = TestCounts(passed=trial.reward, total=1)
        trial.regression_tests = TestCounts(passed=1, total=1)
        trial.feature_failure_kind = "assertion_failure" if not trial.reward else None
        if self.alter_package and trial.role == "control":
            (task_directory / "instruction.md").write_text("different instruction")


class TestEvaluation(ValidationFixture):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"CTF_EVALUATION_DIR": str(self.snapshot_directory.parent / "evaluations")})
        env.start()
        self.addCleanup(env.stop)

    def request(self, *, control=False, repeats=1, agents=None):
        return EvaluationRequest.model_validate({
            "package": self.package(), "mode": "control" if control else "agent", "repeats": repeats,
            "agents": [] if control else (agents or [CONFIG]),
        })

    def evaluate(self, request, backend):
        queued = evaluation_service.create_evaluation(request)
        self.assertEqual(queued.state, "queued")
        result = evaluation_service.run_evaluation(queued.id, request, backend)
        self.assertEqual(evaluation_service.read_evaluation(queued.id), result)
        return result

    def test_equivalent_package_is_used_for_oracle_control_and_balanced_repeats(self):
        backend = FakeBackend()
        config = {**CONFIG, "label": "other", "max_turns": 4}
        result = self.evaluate(self.request(repeats=2, agents=[CONFIG, config]), backend)
        self.assertEqual(result.state, "completed", result.model_dump_json())
        self.assertEqual([(role, label, repeat) for role, label, repeat, _ in backend.runs], [
            ("oracle", "reference", 1), ("control", "untreated", 1),
            ("agent", "small", 1), ("agent", "other", 1), ("agent", "small", 2), ("agent", "other", 2),
        ])
        self.assertEqual(len({path for _, _, _, path in backend.runs}), 1)
        self.assertTrue(self.snapshot_directory.joinpath(result.package.snapshot_id).is_dir())
        self.assertEqual(result.oracle.reward, 1)
        self.assertEqual(result.baseline_control.reward, 0)
        self.assertEqual([item.scheduled for item in result.metrics], [2, 2])
        self.assertTrue(all(item.scored == 0 and item.verified_success.value is None for item in result.metrics))

    def test_oracle_failure_or_weak_control_quarantines_task(self):
        for backend, expected in [(FakeBackend(oracle_failed=True), 1), (FakeBackend(weak_control=True), 2)]:
            with self.subTest(expected=expected):
                result = self.evaluate(self.request(), backend)
                self.assertEqual(result.state, "quarantined")
                self.assertEqual(result.failure, "task_defect")
                self.assertEqual(len(backend.runs), expected)
                self.assertEqual(result.trials, [])

    def test_package_tampering_blocks_before_agent_runs(self):
        backend = FakeBackend(alter_package=True)
        report = self.evaluate(self.request(), backend)
        self.assertEqual(report.state, "blocked")
        self.assertEqual(report.failure, "invalid_evidence")
        self.assertFalse(report.baseline_control.trustworthy)
        self.assertEqual(len(backend.runs), 2)

    def test_provider_unavailability_is_truthful_and_starts_no_harbor_process(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "HF_TOKEN": "", "GEMINI_API_KEY": ""}), patch("app.services.harbor_evaluation._command") as run:
            result = self.evaluate(self.request(), HarborEvaluationBackend())
        run.assert_not_called()
        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.failure, "provider_unavailable")
        self.assertIsNone(result.oracle)
        self.assertIsNone(result.metrics[0].verified_success.value)

    def test_api_returns_receipt_location_and_separate_csv_reports(self):
        request = self.request(control=True)
        local = TestClient(app, client=("127.0.0.1", 41000))
        with patch("app.services.evaluation_service.HarborEvaluationBackend", return_value=FakeBackend()):
            result = local.post("/evaluations", json=request.model_dump(mode="json"))
        self.assertEqual(result.status_code, 202, result.text)
        location = result.headers["location"]
        finished = local.get(location)
        self.assertEqual(finished.status_code, 200)
        self.assertEqual(finished.json()["baseline_control"]["role"], "control")
        self.assertEqual(finished.json()["metrics"], [])
        self.assertIn("verified_success_denominator", local.get(location + "/summary.csv").text)
        self.assertIn("oracle,test_double", local.get(location + "/trials.csv").text)
        self.assertEqual(local.get("/evaluations/" + "f" * 32).status_code, 404)
        remote = TestClient(app, client=("192.0.2.1", 41000))
        self.assertEqual(remote.get(location).status_code, 403)

    def test_rejects_excessive_run_budget_and_unapproved_agent_parameters(self):
        request = self.request().model_dump(mode="json")
        request["agents"] = [{**CONFIG, "label": str(i)} for i in range(3)]
        request["repeats"] = 3
        with self.assertRaisesRegex(ValueError, "six"):
            EvaluationRequest.model_validate(request)
        with self.assertRaises(ValueError):
            AgentConfiguration.model_validate({**CONFIG, "api_base": "https://unexpected.example"})


class TestEvaluationReceipts(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.job = self.root / "jobs" / "agent"
        self.trial = self.job / "task__receipt"
        for path in ("agent", "verifier", "artifacts/submission"):
            (self.trial / path).mkdir(parents=True)
        self.plan = {"groups": [
            {"group": "build", "targets": [], "selectors": []},
            {"group": "original_regression", "targets": ["tests/test_base.py"], "selectors": []},
            {"group": "feature", "targets": ["tests/test_feature.py"],
             "selectors": [{"path": "tests/test_feature.py", "selector": "test_offer"}]},
        ]}
        self.result = {
            "id": TRIAL, "task_checksum": CHECKSUM,
            "agent_info": {"name": "ctf-file-agent", "version": "1.0.0",
                           "model_info": {"name": "example-snapshot", "provider": "openai"}},
            "agent_result": {"n_input_tokens": 300, "n_output_tokens": 40,
                             "metadata": {"ctf_provider_completed_calls": 2, "ctf_resolved_models": ["example-snapshot"]}},
            "verifier_environment_mode": "separate",
            "verifier": {"finished_at": "2026-11-16T12:00:00+00:00"},
            "verifier_result": {"rewards": {"reward": 1}},
        }
        self.write("result.json", self.result)
        self.write("agent/trajectory.json", {"schema_version": "ATIF-v1.8", "steps": [{}],
                                             "agent": {"name": "ctf-file-agent"}})
        self.write("verifier/status.json", {"status": "passed"})
        (self.trial / "verifier/reward.txt").write_text("1\n")
        (self.trial / "artifacts/submission/changed.py").write_text("pass\n")
        rows = []
        for group in self.plan["groups"]:
            cases = [{"path": path, "selector": "test_offer", "outcome": "passed"} for path in group["targets"]]
            name = group["group"]
            rows.append({"group": name, "exit_code": 0, "passed": True, "cases": len(cases)})
            self.write(f"verifier/{name}.json", {"exit_code": 0, "failure_category": None, "cases": cases})
        self.write("verifier/execution.json", rows)

    def write(self, path, payload):
        (self.trial / path).write_text(json.dumps(payload), encoding="utf-8")

    def parse(self):
        evidence = TrialEvidence(role="agent", origin="harbor", label="small", attempt=1,
                                 configuration=AgentConfiguration.model_validate(CONFIG))
        parse_trial(self.root, self.job, evidence, self.plan, {})
        return evidence

    def test_scored_requires_real_provider_and_harbor_reward_receipts(self):
        evidence = self.parse()
        self.assertTrue(evidence.scored)
        self.assertEqual(evidence.feature_tests.total, 1)
        self.assertEqual(evidence.submitted_files[0].operation, "create")
        self.assertEqual(evidence.input_tokens, 300)
        self.assertIsNone(evidence.cost_usd)
        (self.trial / "verifier/reward.txt").write_text("0\n")
        with self.assertRaisesRegex(ValueError, "reward disagree"):
            self.parse()

    def test_missing_provider_execution_or_shared_verifier_cannot_score(self):
        for update in ({"agent_result": {}}, {"verifier_environment_mode": "shared"}):
            with self.subTest(update=update):
                self.write("result.json", {**self.result, **update})
                with self.assertRaises(ValueError):
                    self.parse()

    def test_huggingface_receipts_bind_to_requested_provider_and_model(self):
        configuration = AgentConfiguration(label="hf", model="huggingface/org/model:provider")
        self.result["agent_info"]["model_info"] = {"provider": "huggingface", "name": "org/model:provider"}
        self.result["agent_result"]["metadata"]["ctf_resolved_models"] = ["org/model:provider"]
        self.write("result.json", self.result)
        trial = TrialEvidence(role="agent", origin="harbor", label="hf", attempt=1, configuration=configuration)
        parse_trial(self.root, self.job, trial, self.plan, {})
        self.assertTrue(trial.scored)
        self.assertEqual(trial.resolved_models, ["org/model:provider"])
        self.result["agent_info"]["model_info"]["provider"] = "openai"
        self.write("result.json", self.result)
        with self.assertRaisesRegex(ValueError, "different model"):
            parse_trial(self.root, self.job, trial, self.plan, {})

    def test_timeout_is_not_a_verifier_success(self):
        self.write("result.json", {**self.result, "exception_info": {"exception_type": "AgentTimeoutError"}})
        evidence = self.parse()
        self.assertFalse(evidence.scored)
        self.assertEqual(evidence.failure, "timeout")
        self.assertIsNone(evidence.reward)

    def test_metrics_hold_unknowns_and_report_repeat_uncertainty(self):
        base = self.parse()
        failed = base.model_copy(update={"reward": 0, "feature_tests": TestCounts(passed=0, total=1),
                                         "failure": "feature_failed"})
        timeout = base.model_copy(update={"reward": None, "trustworthy": False, "failure": "timeout"})
        metrics = summarize("small", [base, failed, base, timeout], 4)
        self.assertEqual((metrics.verified_success.numerator, metrics.verified_success.denominator), (2, 3))
        self.assertIsNotNone(metrics.verified_success.wilson95)
        self.assertIsNone(metrics.feature_pass.wilson95)
        self.assertEqual(metrics.feature_pass.value, 2 / 3)
        self.assertEqual(metrics.build_success.value, 1)
        self.assertEqual(metrics.failure_counts, Counter({"feature_failed": 1, "timeout": 1}))
        self.assertIsNone(metrics.cost_usd_total)
