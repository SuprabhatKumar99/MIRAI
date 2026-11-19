"""HTTP contract for native Harbor export and evidence resolution."""

from copy import deepcopy
from unittest.mock import patch

from app.services.harbor_service import PYTHON_IMAGE_ID
from tests.test_docker_validation import ScriptedBackend, ValidationFixture
from tests.test_repository_snapshots import client


class TestHarborEndpoint(ValidationFixture):
    def test_snapshot_backed_download_and_custom_prefix(self):
        payload = self.package()
        response = client.post("/tasks/export/harbor", json={**payload, "task_prefix": "Bench Tasks"})
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else "")
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("bench-tasks-", response.headers["content-disposition"])
        self.assertTrue(response.headers["x-harbor-task-name"].startswith("bench-tasks/"))
        self.assertEqual(response.headers["x-validation-status"], "NOT_VALIDATED")
        self.assertEqual(response.headers["x-repository-snapshot-id"], payload["task"]["snapshot_id"])

    def test_no_snapshot_never_exports_a_head_based_package(self):
        payload = self.package()
        payload["task"]["snapshot_id"] = None
        payload["solution"]["snapshot_id"] = None
        payload["test_suite"]["repository"]["snapshot_id"] = None
        response = client.post("/tasks/export/harbor", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("immutable snapshot", response.json()["detail"])

    def test_report_is_resolved_from_its_id_and_forgery_is_rejected(self):
        payload = self.package()
        backend = ScriptedBackend()
        backend.image_id = PYTHON_IMAGE_ID
        report = self.validate(payload, backend)
        response = client.post("/tasks/export/harbor", json={**payload, "validation_id": report.id})
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else "")
        self.assertEqual(response.headers["x-validation-status"], "VALIDATED")
        self.assertEqual(response.headers["x-validation-id"], report.id)
        report.id = "00000000-0000-0000-0000-000000000001"
        response = client.post("/tasks/export/harbor", json={**payload, "validation_report": report.model_dump()})
        self.assertEqual(response.status_code, 400)
        self.assertIn("server-recorded", response.json()["detail"])

    def test_artifact_id_mismatches_are_rejected(self):
        payload = self.package()
        for field in ("solution", "test_suite"):
            bad = deepcopy(payload)
            bad[field]["task_id"] = "other-task"
            with self.subTest(field=field):
                self.assertEqual(client.post("/tasks/export/harbor", json=bad).status_code, 422)
        report = self.validate(payload, ScriptedBackend()).model_dump()
        report["task_id"] = "other-task"
        self.assertEqual(client.post("/tasks/export/harbor", json={**payload, "validation_report": report}).status_code, 422)

    def test_invalid_or_duplicate_paths_and_credential_paths_are_rejected(self):
        payload = self.package()
        for path in ("../../outside.py", ".env", r"bad\test.py", "a/CON"):
            bad = deepcopy(payload)
            bad["solution"]["files"][0]["path"] = path
            with self.subTest(path=path):
                self.assertEqual(client.post("/tasks/export/harbor", json=bad).status_code, 400)
        payload["solution"]["files"].append(payload["solution"]["files"][0])
        response = client.post("/tasks/export/harbor", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Duplicate", response.json()["detail"])

    @patch("app.services.harbor_service.export_harbor_task_package", side_effect=OSError("private host path"))
    def test_internal_failure_does_not_leak_host_paths(self, _):
        response = client.post("/tasks/export/harbor", json=self.package())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private host path", response.json()["detail"])

    def test_existing_endpoints_are_available(self):
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/tasks/").status_code, 200)
