"""Run the adapter against official Harbor types with a scripted provider."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HAS_HARBOR = importlib.util.find_spec("harbor") is not None


@unittest.skipUnless(HAS_HARBOR, "Requires the optional Harbor 0.25.0 Python 3.12 runtime")
class TestHarborAgentContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "app/services/harbor_ctf_agent.py"
        spec = importlib.util.spec_from_file_location("harbor_ctf_agent", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        from harbor.models.agent.context import AgentContext

        self.context = AgentContext()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.logs = Path(temp.name)
        self.agent = self.module.CTFFileAgent(logs_dir=self.logs, model_name="openai/gpt-4.1-mini", max_turns=3)
        self.actions = []
        self.requests = []
        self.replies = []

        async def command(argv, **kwargs):
            arguments = shlex.split(argv)
            self.actions.append(json.loads(base64.b64decode(arguments[-1])))
            self.assertEqual(arguments[0], "/usr/local/bin/python")
            self.assertEqual(kwargs["cwd"], "/app")
            self.assertNotIn("env", kwargs)
            return SimpleNamespace(return_code=0, stdout="fixture output", stderr="")

        self.environment = SimpleNamespace(exec=command)

    def provider(self, **kwargs):
        expected = getattr(self, "expected_endpoint", "https://api.openai.com/v1")
        self.assertEqual(kwargs["base_url"], expected)
        self.assertEqual(kwargs["max_retries"], 0)

        async def complete(**settings):
            self.requests.append(settings)
            response = self.replies.pop(0)
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(
                model=getattr(self, "resolved_model", "gpt-4.1-mini-2025-04-14"),
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))],
                usage=SimpleNamespace(prompt_tokens=30, completion_tokens=5, prompt_tokens_details=None),
            )

        async def enter(_):
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))

        async def close(*_):
            await kwargs["http_client"].aclose()

        return type("MockProvider", (), {"__aenter__": enter, "__aexit__": close})()

    async def test_records_provider_usage_and_atif_without_forwarding_credentials(self):
        self.replies[:] = [{"op": "read", "path": "main.py"}, {"op": "finish"}]
        with patch.dict(os.environ, {self.module.PROVIDER_KEY: "fixture-only"}), \
                patch.object(self.module, "AsyncOpenAI", side_effect=self.provider):
            await self.agent.setup(self.environment)
            await self.agent.run("Change the return value.", self.environment, self.context)
        self.assertEqual(self.actions, [{"op": "probe"}, {"op": "read", "path": "main.py"}])
        self.assertEqual(self.requests[0]["model"], "gpt-4.1-mini")
        self.assertEqual(self.context.n_input_tokens, 60)
        self.assertEqual(self.context.n_output_tokens, 10)
        self.assertEqual(self.context.metadata["ctf_provider_completed_calls"], 2)
        trajectory = json.loads((self.logs / "trajectory.json").read_bytes())
        self.assertEqual(trajectory["schema_version"], "ATIF-v1.8")
        self.assertEqual(trajectory["agent"]["model_name"], "gpt-4.1-mini")
        self.assertEqual(trajectory["final_metrics"]["total_completion_tokens"], 10)
        self.assertEqual(trajectory["steps"][1]["message"], "Change the return value.")

    async def test_unavailable_provider_has_no_successful_completion(self):
        import httpx
        from openai import APIConnectionError

        self.replies[:] = [APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))]
        with patch.dict(os.environ, {self.module.PROVIDER_KEY: "fixture-only"}), \
                patch.object(self.module, "AsyncOpenAI", side_effect=self.provider):
            with self.assertRaises(self.module.AgentProviderUnavailable):
                await self.agent.run("Change the return value.", self.environment, self.context)
        self.assertEqual(self.context.metadata["ctf_provider_completed_calls"], 0)
        self.assertIsNone(self.context.n_output_tokens)

    async def test_huggingface_uses_fixed_router_and_records_model_and_usage(self):
        self.expected_endpoint = "https://router.huggingface.co/v1"
        self.resolved_model = "example/model:provider"
        self.agent = self.module.CTFFileAgent(
            logs_dir=self.logs, model_name="huggingface/example/model:provider", max_turns=1,
        )
        self.replies[:] = [{"op": "finish"}]
        with patch.dict(os.environ, {self.module.PROVIDER_KEY: "fixture-hf-token"}), \
                patch.object(self.module, "AsyncOpenAI", side_effect=self.provider):
            await self.agent.setup(self.environment)
            await self.agent.run("Implement the public task.", self.environment, self.context)
        self.assertEqual(self.requests[0]["model"], "example/model:provider")
        self.assertEqual(self.requests[0]["max_tokens"], 1024)
        self.assertNotIn("max_completion_tokens", self.requests[0])
        self.assertEqual(self.context.metadata["ctf_resolved_models"], [self.resolved_model])
        self.assertEqual(self.context.n_output_tokens, 5)
        self.assertEqual(self.actions, [{"op": "probe"}])

    async def test_provider_permission_failure_records_only_status(self):
        import httpx
        from openai import PermissionDeniedError

        self.replies[:] = [PermissionDeniedError(
            "private upstream response",
            response=httpx.Response(403, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
            body=None,
        )]
        with patch.dict(os.environ, {self.module.PROVIDER_KEY: "fixture-only"}), \
                patch.object(self.module, "AsyncOpenAI", side_effect=self.provider):
            with self.assertRaises(self.module.AgentProviderUnavailable) as caught:
                await self.agent.run("Implement the task.", self.environment, self.context)
        self.assertEqual(self.context.metadata["ctf_provider_error_code"], 403)
        self.assertNotIn("private upstream response", str(caught.exception))

    async def test_gemini_uses_existing_compatibility_adapter(self):
        self.expected_endpoint = "https://generativelanguage.googleapis.com/v1beta/openai/"
        self.resolved_model = "gemini-3.5-flash-lite"
        self.agent = self.module.CTFFileAgent(
            logs_dir=self.logs, model_name="gemini/gemini-3.5-flash-lite", max_turns=1,
        )
        self.replies[:] = [{"op": "finish"}]
        with patch.dict(os.environ, {self.module.PROVIDER_KEY: "gemini-fixture"}), \
                patch.object(self.module, "AsyncOpenAI", side_effect=self.provider):
            await self.agent.setup(self.environment)
            await self.agent.run("Implement the public task.", self.environment, self.context)
        self.assertEqual(self.requests[0]["model"], "gemini-3.5-flash-lite")
        self.assertEqual(self.requests[0]["max_tokens"], 1024)
        self.assertEqual(self.context.metadata["ctf_resolved_models"], [self.resolved_model])
        self.assertEqual(self.context.metadata["ctf_provider_completed_calls"], 1)
