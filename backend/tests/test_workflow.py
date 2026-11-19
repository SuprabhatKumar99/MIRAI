"""Test durable workflow and evidence access without live ML calls."""

import json
import os
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.evaluation import EvaluationReport
from app.schemas.solution import SolutionResponse
from app.schemas.task import TaskResponse
from app.schemas.test_suite import TestSuiteResponse
from app.services import harbor_service, workflow_jobs, workflow_store as store
from tests.test_docker_validation import ScriptedBackend, ValidationFixture
from tests.test_repository_snapshots import upload, zip_bytes


class TestWorkflow(ValidationFixture):
    def setUp(self):
        super().setUp()
        self.root = self.snapshot_directory.parent / "workflow"
        self.client = TestClient(app, base_url="https://testserver")
        self.addCleanup(self.client.close)
        env = patch.dict(os.environ, {
            "CTF_STATE_DIR": str(self.root),
            "CTF_REQUESTS_PER_MINUTE": "0",
            "CTF_MODEL_REQUESTS_PER_DAY": "0",
            "CTF_GLOBAL_MODEL_REQUESTS_PER_DAY": "0",
            "CTF_SHARED_MODE": "0",
        })
        env.start()
        self.addCleanup(env.stop)

    def seed(self):
        fixture = self.package()
        task = TaskResponse.model_validate(fixture["task"])
        with patch("app.services.task_service.generate_task_from_repo", return_value=task):
            response = self.client.post("/tasks/generate/from-repo", json={"snapshot_id": task.snapshot_id})
        self.assertEqual(response.status_code, 201, response.text)
        return fixture

    def job(self, task_id, kind, revision, **options):
        return self.client.post(f"/tasks/{task_id}/jobs", json={"kind": kind, "revision": revision, **options})

    def detail(self, task_id):
        result = self.client.get(f"/tasks/{task_id}")
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_catalogue_reopens_and_filters(self):
        fixture = self.seed()
        task = fixture["task"]
        identifier = task["id"]
        with TestClient(app) as new_client:
            projects = new_client.get("/projects").json()["projects"]
            self.assertEqual(len(projects), 1)
            same = new_client.get("/snapshots/" + task["snapshot_id"]).json()
            self.assertEqual(projects[0]["snapshot"], same)
            task_list = new_client.get("/tasks/", params={
                "q": task["title"][:10], "difficulty": task["difficulty"], "project_id": projects[0]["id"], "limit": 1,
            }).json()
            self.assertEqual(task_list["total"], 1)
            self.assertEqual(task_list["tasks"][0]["id"], identifier)
            self.assertIsNone(task_list["states"][0]["verdict"])
            self.assertEqual(new_client.get("/tasks/?offset=1").json()["tasks"], [])
            self.assertEqual(new_client.get("/tasks/?q=unknown-task-name").json()["total"], 0)
            self.assertEqual(new_client.get("/tasks/" + identifier).json()["snapshot"], same)
        with store.database() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], len(store._MIGRATIONS))
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_migrate_an_existing_catalogue(self):
        from sqlite3 import connect
        self.root.mkdir()
        with connect(self.root / "workflow.sqlite3") as connection:
            connection.executescript(store._MIGRATIONS[0])
            connection.execute("PRAGMA user_version=1")
            connection.execute("INSERT INTO projects VALUES (?,?,?,?,?)", ("local", "legacy", "repo", "old", "2026-01-01"))
        with store.database() as connection:
            self.assertEqual(connection.execute("SELECT name FROM projects").fetchone()[0], "old")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], len(store._MIGRATIONS))
            self.assertIn("request_key", [column["name"] for column in connection.execute("PRAGMA table_info(jobs)")])

    def test_missing_snapshot_reports_recovery_and_does_not_call_provider(self):
        fixture = self.seed()
        task = fixture["task"]
        archive_dir = self.snapshot_directory / task["snapshot_id"]
        archive_dir.rename(self.snapshot_directory.parent / "snapshot-backup")

        with patch("app.services.solution_service._get_gemini_client") as provider:
            queued = self.job(task["id"], "solution", 0)
            self.assertEqual(queued.status_code, 202, queued.text)
            job = self.client.get(queued.headers["location"]).json()
            provider.assert_not_called()
        self.assertEqual(job["state"], "failed")
        self.assertIn("Re-upload the original ZIP", job["error"])
        self.assertIsNone(self.detail(task["id"])["solution"])

    def test_managed_artifacts_evidence_and_stale_revisions(self):
        fixture = self.seed()
        task_id = fixture["task"]["id"]
        solution = SolutionResponse.model_validate(fixture["solution"])
        suite = TestSuiteResponse.model_validate(fixture["test_suite"])
        self.assertEqual(self.job(task_id, "validation", 0).status_code, 409)
        forged = self.job(task_id, "validation", 0, overall_verdict="VALIDATED")
        self.assertEqual(forged.status_code, 422)
        with patch("app.services.solution_service.generate_reference_solution", return_value=solution) as generate:
            first = self.job(task_id, "solution", 0, request_key="persistent-request-01")
            second = self.job(task_id, "solution", 0, request_key="persistent-request-01")
            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(first.json()["id"], second.json()["id"])
            self.assertEqual(second.json()["state"], "completed")
            generate.assert_called_once_with(TaskResponse.model_validate(fixture["task"]))
        self.assertEqual(self.job(task_id, "tests", 0).status_code, 409)
        with patch("app.services.test_service.generate_test_suite", return_value=suite) as generate:
            tests = self.job(task_id, "tests", 1)
            self.assertEqual(tests.status_code, 202, tests.text)
            self.assertEqual(generate.call_args.kwargs["solution"], solution)
        self.assertEqual(self.job(task_id, "evaluation", 2).status_code, 409)
        sandbox = ScriptedBackend()
        sandbox.image_id = harbor_service.PYTHON_IMAGE_ID
        with patch("app.services.validation_service.validate_task_package",
                   side_effect=lambda _root, payload: self.validate(payload.model_dump(mode="json"), sandbox)):
            validation_job = self.job(task_id, "validation", 2)
        self.assertEqual(validation_job.status_code, 202, validation_job.text)
        saved = self.detail(task_id)
        report = saved["validation"]
        self.assertEqual(report["overall_verdict"], "VALIDATED")
        self.assertEqual(saved["revision"], 3)
        self.assertEqual(len(report["gates"]), 5)
        log = Path(report["gates"][0]["runs"][0]["log_location"]).name
        self.assertIn("fixture log", self.client.get(f"/history/validation/{report['id']}/logs/{log}").text)
        self.assertEqual(self.client.get(f"/history/validation/{report['id']}/logs/report.json").status_code, 404)

        evaluation = EvaluationReport(
            id="e" * 32, mode="control", agents=[], repeats=1, task_id=task_id,
            state="blocked", failure="environment_failure",
        )
        with patch("app.services.evaluation_service.create_evaluation", return_value=evaluation), patch(
            "app.services.evaluation_service.run_evaluation", return_value=evaluation,
        ) as run:
            response = self.job(task_id, "evaluation", 3)
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(run.call_args.args[1].package.validation_id, report["id"])
        self.assertEqual(self.client.get(f"/history/evaluation/{evaluation.id}").json()["state"], "blocked")
        self.assertEqual(self.client.get(f"/history/evaluation/{evaluation.id}/summary.csv").status_code, 200)

        response = self.job(task_id, "export", 3)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.client.get(response.headers["location"]).json()["state"], "completed")
        saved_export = self.detail(task_id)["export"]
        self.assertEqual(saved_export["validation_status_recorded"], "VALIDATED")
        self.assertEqual(saved_export["validation_id"], report["id"])
        self.assertNotIn("archive_path", saved_export)
        url = f"/history/export/{saved_export['id']}/download"
        download = self.client.get(url)
        self.assertEqual(download.status_code, 200)
        with ZipFile(BytesIO(download.content)) as archive:
            self.assertTrue(any(path.endswith("/task.toml") for path in archive.namelist()))
        (self.root / "exports" / saved_export["id"] / saved_export["filename"]).write_bytes(b"mutated")
        self.assertEqual(self.client.get(url).status_code, 409)

        next_solution = solution.model_copy(update={"id": "sample-new-solution"})
        with patch("app.services.solution_service.generate_reference_solution", return_value=next_solution):
            result = self.job(task_id, "solution", 3)
            self.assertEqual(result.status_code, 202, result.text)
        after = self.detail(task_id)
        self.assertEqual(after["solution"]["id"], next_solution.id)
        for key in ("validation", "test_suite", "export"):
            self.assertIsNone(after[key])
        history = self.client.get(f"/tasks/{task_id}/history/validation").json()["items"]
        self.assertEqual(history[0]["id"], report["id"])
        self.assertEqual(self.client.get(f"/history/validation/{report['id']}").status_code, 200)

    def test_queue_cancel_and_restart(self):
        fixture = self.seed()
        task_id = fixture["task"]["id"]
        with patch.object(workflow_jobs, "drain_jobs"):
            job = self.job(task_id, "solution", 0)
            duplicate = self.job(task_id, "solution", 0)
            self.assertEqual(job.status_code, 202, job.text)
            self.assertEqual(job.json()["id"], duplicate.json()["id"])
            url = job.headers["location"]
            self.assertEqual(self.client.delete(url).json()["state"], "cancelled")
            self.assertIsNone(store.claim())
            job = self.job(task_id, "solution", 0)
            self.assertEqual(store.claim()["id"], job.json()["id"])
        workflow_jobs.drain_jobs()
        response = self.client.get(job.headers["location"])
        self.assertEqual(response.json()["state"], "interrupted")
        self.assertIsNone(self.detail(task_id)["solution"])

    def test_running_cancel_discards_artifact_and_global_budget_is_atomic(self):
        fixture = self.seed()
        task_id = fixture["task"]["id"]
        with patch.object(workflow_jobs, "drain_jobs"):
            response = self.job(task_id, "solution", 0)
        running = store.claim()
        self.assertEqual(self.client.delete(response.headers["location"]).json()["state"], "running")
        solution = SolutionResponse.model_validate(fixture["solution"])
        store.finish(running, (solution.id, solution.model_dump(mode="json"), None, None))
        self.assertEqual(self.client.get(response.headers["location"]).json()["state"], "cancelled")
        self.assertIsNone(self.detail(task_id)["solution"])
        self.assertEqual(self.client.get(f"/history/solution/{solution.id}").status_code, 404)

        bucket = store.now()[:10] + ":global-test"
        self.assertTrue(store.reserve("alice", bucket, 2, 4, global_limit=3))
        self.assertFalse(store.reserve("bob", bucket, 2, 4, global_limit=3))
        self.assertTrue(store.reserve("bob", bucket, 1, 1, global_limit=3))
        self.assertFalse(store.reserve("carol", bucket, 1, 4, global_limit=3))

    def test_shared_mode_isolates_tasks_snapshots_jobs_and_artifacts(self):
        fixture = self.seed()
        task = TaskResponse.model_validate(fixture["task"])
        tokens = {"alice": "a" * 36, "bob": "b" * 36}
        alice = {"Authorization": "Bearer " + tokens["alice"]}
        bob = {"Authorization": "Bearer " + tokens["bob"]}
        snapshot = store.snapshot("local", task.snapshot_id)
        store.save_task("alice", task, self._analysis(snapshot))
        solution = SolutionResponse.model_validate(fixture["solution"])
        store.attach_legacy("alice", task, solution, "solution", 0)
        with patch.dict(os.environ, {"CTF_SHARED_MODE": "1", "CTF_ACCESS_TOKENS": json.dumps(tokens)}):
            with patch.object(workflow_jobs, "drain_jobs"):
                queued = self.client.post(f"/tasks/{task.id}/jobs", headers=alice, json={"kind": "solution", "revision": 1})
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertEqual(self.client.get(f"/tasks/{task.id}").status_code, 401)
            self.assertEqual(self.client.get("/tasks/", headers=bob).json()["tasks"], [])
            for path in (f"/tasks/{task.id}", f"/snapshots/{task.snapshot_id}", f"/history/solution/{solution.id}",
                         f"/tasks/{task.id}/history/validation", queued.headers["location"]):
                self.assertEqual(self.client.get(path, headers=bob).status_code, 404, path)
                self.assertEqual(self.client.get(path, headers=alice).status_code, 200, path)
            self.assertEqual(self.client.delete(queued.headers["location"], headers=bob).status_code, 404)
            self.assertEqual(self.client.get("/docs", headers=alice).status_code, 404)
            self.assertEqual(self.client.get("/projects", headers={"Authorization": tokens["alice"]}).status_code, 401)
            login = self.client.post("/auth/session", headers=alice)
            self.assertEqual(login.status_code, 200)
            for flag in ("Secure", "HttpOnly", "SameSite=strict"):
                self.assertIn(flag, login.headers["set-cookie"])
            self.assertEqual(self.client.get("/projects").status_code, 200)
            self.assertEqual(self.client.delete(queued.headers["location"], headers={"Origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(self.client.post("/tasks/validate", headers=alice, json=fixture).status_code, 409)
            disallowed = self.client.post(f"/tasks/{task.id}/jobs", headers=alice, json={
                "kind": "evaluation", "revision": 1, "mode": "agent",
                "agents": [{"label": "probe", "model": "openai/unlisted"}],
            })
            self.assertEqual(disallowed.status_code, 403)

    def test_quota_payload_cap_and_disallowed_origin(self):
        with patch.dict(os.environ, {"CTF_REQUESTS_PER_MINUTE": "1"}):
            self.assertEqual(self.client.get("/projects").status_code, 200)
            self.assertEqual(self.client.get("/projects").status_code, 429)
        with patch.dict(os.environ, {"CTF_MODEL_REQUESTS_PER_DAY": "1"}):
            with patch("app.services.task_service.generate_task") as provider:
                response = self.client.post("/tasks/generate", json={"topic": "Demo"})
                self.assertEqual(response.status_code, 429)
                provider.assert_not_called()
        response = self.client.post("/tasks/generate", content=(b"a" * (1024 * 1024) for _ in range(5)))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.post("/tasks/generate", headers={"Content-Encoding": "gzip"}).status_code, 415)
        self.assertEqual(self.client.post("/tasks/generate", headers={"Origin": "https://evil.example"}).status_code, 403)

    @staticmethod
    def _analysis(value):
        from app.schemas.repo import RepoAnalysisResponse
        return RepoAnalysisResponse.model_validate(value)
