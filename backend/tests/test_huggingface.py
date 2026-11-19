"""Credential-free Hugging Face admission, fallback and isolation checks."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError

from app.schemas.evaluation import AgentConfiguration, TrialEvidence
from app.services.harbor_evaluation import HarborEvaluationBackend, _controller_environment, utcnow


class TestHuggingFace(TestCase):
    def test_gemini_credential_is_forwarded_only_to_selected_provider(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-fixture", "HF_TOKEN": "hf-fixture"}):
            env = _controller_environment(provider=True, model="gemini/gemini-3.5-flash-lite")
            self.assertEqual(env["CTF_EVAL_PROVIDER_KEY"], "gemini-fixture")
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertNotIn("HF_TOKEN", env)
        config = AgentConfiguration(label="sample", model="huggingface/org/model",
                                    fallback_model="gemini/gemini-3.5-flash-lite")
        self.assertEqual(config.fallback_model, "gemini/gemini-3.5-flash-lite")
        backend = HarborEvaluationBackend()
        backend.executable = "harbor"
        trial = TrialEvidence(role="agent", origin="harbor", label="sample", attempt=1, configuration=config)
        timestamp = utcnow()
        with TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HF_TOKEN": "", "GEMINI_API_KEY": "gemini-fixture"},
        ), patch("app.services.harbor_evaluation._command", return_value=(
            1, b"provider unavailable", False, False, timestamp, timestamp,
        )) as command, patch("app.services.harbor_evaluation.parse_trial", side_effect=ValueError):
            root = Path(directory)
            backend.run(root / "task", root, trial, {}, {})
            self.assertIn("gemini/gemini-3.5-flash-lite", command.call_args.args[0])
            self.assertEqual(command.call_args.args[3]["CTF_EVAL_PROVIDER_KEY"], "gemini-fixture")
        self.assertEqual(trial.requested_model, "huggingface/org/model")
        self.assertEqual(trial.configuration.model, "gemini/gemini-3.5-flash-lite")
        self.assertIn("gemini_fallback_primary_key_missing", trial.flags)

    def configuration(self, **kwargs):
        return AgentConfiguration(label="sample", model="openai/test", fallback_model="huggingface/org/model:provider", **kwargs)

    def test_model_contract_and_invalid_paths(self):
        self.assertEqual(self.configuration().fallback_model, "huggingface/org/model:provider")
        for model in ("huggingface/../model", "huggingface/org/model/extra", "https://attacker/model", "huggingface/org/model?x=y"):
            with self.subTest(model=model), self.assertRaises(ValidationError):
                AgentConfiguration(label="sample", model=model)
        with self.assertRaises(ValidationError):
            AgentConfiguration(label="sample", model="huggingface/org/model", fallback_model="huggingface/other/model")

    def test_only_selected_credential_is_forwarded(self):
        with patch.dict(os.environ, {"HF_TOKEN": "hf-fixture", "OPENAI_API_KEY": "openai-fixture"}):
            hf = _controller_environment(provider=True, model="huggingface/org/model")
            primary = _controller_environment(provider=True)
            control = _controller_environment()
        self.assertEqual(hf["CTF_EVAL_PROVIDER_KEY"], "hf-fixture")
        self.assertEqual(primary["CTF_EVAL_PROVIDER_KEY"], "openai-fixture")
        for environment in (hf, primary, control):
            self.assertNotIn("HF_TOKEN", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CTF_EVAL_PROVIDER_KEY", control)

    def test_missing_openai_key_selects_explicit_fallback_and_redacts_logs(self):
        trial = TrialEvidence(role="agent", origin="harbor", label="sample", attempt=1,
                              configuration=self.configuration())
        backend = HarborEvaluationBackend()
        backend.executable = "harbor"
        timestamp = utcnow()
        with TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HF_TOKEN": "hf-fixture-secret", "OPENAI_API_KEY": ""},
        ), patch("app.services.harbor_evaluation._command", return_value=(
            1, b"hf-fixture-secret upstream error", False, False, timestamp, timestamp,
        )) as command, patch("app.services.harbor_evaluation.parse_trial", side_effect=ValueError):
            root = Path(directory)
            backend.run(root / "task", root, trial, {}, {})
            self.assertIn("huggingface/org/model:provider", command.call_args.args[0])
            self.assertEqual(command.call_args.args[3]["CTF_EVAL_PROVIDER_KEY"], "hf-fixture-secret")
            self.assertNotIn("hf-fixture-secret", (root / trial.process.console_log).read_text())
        self.assertEqual(trial.requested_model, "openai/test")
        self.assertEqual(trial.configuration.model, "huggingface/org/model:provider")
        self.assertIn("huggingface_fallback_openai_key_missing", trial.flags)
        self.assertIsNone(trial.reward)

    def test_primary_is_preserved_and_missing_hf_fails_closed(self):
        backend = HarborEvaluationBackend()
        backend.executable = "harbor"
        trial = TrialEvidence(role="agent", origin="harbor", label="sample", attempt=1,
                              configuration=AgentConfiguration(label="sample", model="huggingface/org/model"))
        with patch.dict(os.environ, {"HF_TOKEN": "", "OPENAI_API_KEY": "openai-fixture"}), \
                patch("app.services.harbor_evaluation._command") as command:
            backend.run(Path("."), Path("."), trial, {}, {})
            command.assert_not_called()
        self.assertEqual(trial.failure, "provider_unavailable")

    def test_configured_openai_primary_is_not_replaced(self):
        trial = TrialEvidence(role="agent", origin="harbor", label="sample", attempt=1,
                              configuration=self.configuration())
        backend = HarborEvaluationBackend()
        backend.executable = "harbor"
        timestamp = utcnow()
        with TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HF_TOKEN": "hf-fixture", "OPENAI_API_KEY": "openai-fixture"},
        ), patch("app.services.harbor_evaluation._command", return_value=(
            1, b"upstream error", False, False, timestamp, timestamp,
        )) as command, patch("app.services.harbor_evaluation.parse_trial", side_effect=ValueError):
            root = Path(directory)
            backend.run(root / "task", root, trial, {}, {})
            self.assertIn("openai/test", command.call_args.args[0])
            self.assertEqual(command.call_args.args[3]["CTF_EVAL_PROVIDER_KEY"], "openai-fixture")
        self.assertIsNone(trial.requested_model)
        self.assertEqual(trial.configuration.model, "openai/test")
