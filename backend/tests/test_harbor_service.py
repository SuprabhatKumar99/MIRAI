"""Verify the standalone Harbor package, receipt binding, and oracle filesystem policy."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import tomllib
from unittest.mock import patch
from zipfile import ZipFile

from app.schemas.harbor import HarborExportRequest, HarborMetadataConfig, HarborTaskInfo, HarborTaskManifest
from app.services import harbor_guest, harbor_service
from tests.test_docker_validation import FIXED, ORIGINAL, ScriptedBackend, ValidationFixture
from tests.repository_fixtures import GITHUB_COMMIT, github_archive_responses
from tests.test_repository_snapshots import client


class TestHarborService(ValidationFixture):
    def export(self, payload, directory):
        return harbor_service.export_harbor_task_package(HarborExportRequest.model_validate(payload), directory)

    def test_unsafe_paths_are_rejected_and_unicode_nested_paths_are_preserved(self):
        allowed = "project/tests/it's café/test_sample.py"
        self.assertEqual(harbor_service.validate_export_file_path(allowed).as_posix(), allowed)
        for path in ("../test.py", "a/./b", "a//b", "/test.py", r"C:\test.py", r"x\y", "CON.py",
                     ".env", "a/.aws/config", "a/private.pem", "a/file.pyc"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                harbor_service.validate_export_file_path(path)

    def test_toml_round_trips_unicode_and_shell_metacharacters_as_data(self):
        title = 'Quotes "two", backslash \\ and Unicode café 🔐\nsecond line'
        manifest = HarborTaskManifest(
            task=HarborTaskInfo(name="ctf/demo", description=title),
            metadata=HarborMetadataConfig(project_name=title),
        )
        data = tomllib.loads(harbor_service.generate_task_toml(manifest))
        self.assertEqual(data["task"]["description"], title)
        self.assertEqual(data["metadata"]["project_name"], title)
        self.assertEqual(data["schema_version"], "1.4")
        self.assertEqual(data["metadata"]["network_policy"], "docker-compose:none")
        self.assertEqual(data["verifier"]["environment_mode"], "separate")
        self.assertEqual(data["artifacts"], [{"source": "/app", "destination": "submission"}])

    def test_archive_has_separate_contexts_exact_bytes_and_executable_scripts(self):
        original = b'{"unit":"\xc3\xa9"}\r\n'
        payload = self.package(files={"repository/tests/fixtures/café.json": original})
        with tempfile.TemporaryDirectory() as directory:
            path, report = self.export(payload, directory)
            root = Path(report.export_directory)
            self.assertEqual(report.validation_status_recorded, "NOT_VALIDATED")
            self.assertTrue((root / "task.toml").is_file())
            manifest = json.loads((root / "environment/snapshot.json").read_bytes())
            self.assertEqual(manifest["snapshot_id"], payload["task"]["snapshot_id"])
            self.assertEqual(manifest["archive_sha256"], report.snapshot_sha256)
            self.assertIsNone(manifest["commit_sha"])
            self.assertNotIn("starting_commit_sha", tomllib.loads((root / "task.toml").read_text())["metadata"])
            self.assertEqual(manifest["files"]["tests/fixtures/café.json"], hashlib.sha256(original).hexdigest())
            self.assertEqual((root / "environment/source/tests/fixtures/café.json").read_bytes(), original)
            self.assertNotIn("class Feature", (root / "instruction.md").read_text())
            self.assertFalse((root / "environment/source/tests/test_feature.py").exists())
            private = root / "tests/private/feature/tests/test_feature.py"
            self.assertEqual(private.read_bytes(), payload["test_suite"]["feature_test_files"][0]["code"].encode())
            plan = json.loads((root / "tests/plan.json").read_bytes())
            self.assertIn("tests/fixtures/café.json", plan["original_inputs"])
            self.assertEqual(plan["groups"][2]["selectors"], [
                {"path": "tests/test_feature.py", "selector": "Feature.test_reservation"},
            ])
            self.assertNotIn("git clone", (root / "environment/Dockerfile").read_text())
            self.assertNotIn("source/", (root / "tests/Dockerfile").read_text())
            for context in ("environment", "tests"):
                self.assertIn('network_mode: "none"', (root / context / "docker-compose.yaml").read_text())
            with ZipFile(path) as archive:
                self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
                for script in ("tests/test.sh", "solution/solve.sh"):
                    name = f"{root.name}/{script}"
                    self.assertEqual((archive.getinfo(name).external_attr >> 16) & 0o777, 0o755)
                    self.assertNotIn(b"\r", archive.read(name))

    def test_oracle_verifies_before_and_after_honors_deleted_files_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            root.mkdir()
            oracle = root.parent / "solution"
            (oracle / "files").mkdir(parents=True)
            (root / "initial.py").write_bytes(b"before")
            (root / "old.py").write_bytes(b"discard")
            replacement = FIXED.replace("\n", "\r\n").encode()
            (oracle / "files/000.blob").write_bytes(replacement)
            (oracle / "files/001.blob").write_bytes(b"new")
            changes = [
                {"path": "initial.py", "before": harbor_guest.digest(root / "initial.py"),
                 "after": harbor_guest.digest(oracle / "files/000.blob"), "payload": "000.blob"},
                {"path": "added.py", "before": None, "after": harbor_guest.digest(oracle / "files/001.blob"),
                 "payload": "001.blob"},
                {"path": "old.py", "before": harbor_guest.digest(root / "old.py"), "after": None},
            ]
            plan = oracle / "changes.json"
            plan.write_text(json.dumps(changes))
            sha = harbor_guest.digest(plan)
            with patch.object(harbor_guest, "ROOT", root):
                (root / "initial.py").write_bytes(b"conflict")
                with self.assertRaisesRegex(ValueError, "starting-file"):
                    harbor_guest.apply_oracle(oracle, sha)
                self.assertFalse((root / "added.py").exists())
                (root / "initial.py").write_bytes(b"before")
                harbor_guest.apply_oracle(oracle, sha)
                harbor_guest.apply_oracle(oracle, sha)
            self.assertEqual((root / "initial.py").read_bytes(), replacement)
            self.assertEqual((root / "added.py").read_bytes(), b"new")
            self.assertFalse((root / "old.py").exists())

    def test_packaged_github_revision_comes_from_the_snapshot(self):
        def github_upload(archive):
            with (
                patch("app.services.repo_service._verify_hostname_and_dns", return_value="140.82.112.6"),
                patch("app.services.repo_service.requests.Session.get",
                      side_effect=github_archive_responses("owner/repo", archive, sha=GITHUB_COMMIT)),
            ):
                return client.post("/tasks/analyze/github", json={"repo_url": "https://github.com/owner/repo"})

        with patch("tests.test_docker_validation.upload", side_effect=github_upload):
            payload = self.package()
        payload["task"]["starting_commit_sha"] = "f" * 40
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.export(payload, directory)
            root = Path(report.export_directory)
            manifest = json.loads((root / "environment/snapshot.json").read_bytes())
            self.assertEqual(manifest["commit_sha"], GITHUB_COMMIT)
            self.assertEqual(manifest["archive_sha256"], report.snapshot_sha256)
            task = tomllib.loads((root / "task.toml").read_text())
            self.assertEqual(task["metadata"]["starting_commit_sha"], GITHUB_COMMIT)

    def test_source_verification_rejects_changed_files_and_symlink_submissions(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.export(self.package(), directory)
            environment = Path(report.export_directory) / "environment"
            source = environment / "source"
            manifest = environment / "snapshot.json"
            with patch.object(harbor_guest, "ROOT", source):
                checksum = harbor_guest.digest(manifest)
                harbor_guest.verify_source(manifest, checksum)
                (source / "warehouse.py").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "starting tree"):
                    harbor_guest.verify_source(manifest, checksum)
            link = source / "linked.py"
            try:
                link.symlink_to(source / "warehouse.py")
            except OSError:
                self.skipTest("Symlink creation is unavailable in this test environment")
            with self.assertRaisesRegex(ValueError, "links"):
                harbor_guest.tree(source)

    def test_windows_transfer_recovers_exact_original_bytes_and_rejects_edits(self):
        original = ORIGINAL.replace("\n", "\r\n")
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.export(self.package(original=original), directory)
            package = Path(report.export_directory)
            plan = json.loads((package / "tests/plan.json").read_bytes())
            relative = "tests/test_original.py"
            uploaded = package / "environment" / "source" / relative
            uploaded.write_bytes(ORIGINAL.encode())
            restored = harbor_guest.verified_regression_inputs(package / "tests", plan, {relative: uploaded})
            self.assertEqual(restored[relative].read_bytes(), original.encode())
            uploaded.write_bytes(ORIGINAL.replace("stable(), 5", "stable(), 6").encode())
            with self.assertRaisesRegex(ValueError, "fixtures changed"):
                harbor_guest.verified_regression_inputs(package / "tests", plan, {relative: uploaded})

    def test_server_evidence_binds_snapshot_artifacts_image_and_lock(self):
        payload = self.package()
        backend = ScriptedBackend()
        backend.image_id = harbor_service.PYTHON_IMAGE_ID
        report = self.validate(payload, backend)
        self.assertEqual(report.overall_verdict, "VALIDATED")
        payload["validation_id"] = report.id
        with tempfile.TemporaryDirectory() as directory:
            _, exported = self.export(payload, Path(directory) / "valid")
            self.assertEqual(exported.validation_status_recorded, "VALIDATED")
            self.assertEqual(exported.validation_id, report.id)
            for field in ("task", "solution", "test_suite"):
                tampered = deepcopy(payload)
                tampered[field]["title" if field == "task" else "task_title"] = "Changed"
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "server-recorded"):
                    self.export(tampered, Path(directory) / f"bad-{field}")
            original_record = self.reports / report.evidence_location
            report.dependency_lock_sha256 = "a" * 64
            original_record.write_text(report.model_dump_json())
            with self.assertRaisesRegex(ValueError, "different runtime or dependency lock"):
                self.export(payload, Path(directory) / "wrong-lock")
            report.dependency_lock_sha256 = hashlib.sha256(b"").hexdigest()
            report.snapshot_sha256 = "b" * 64
            original_record.write_text(report.model_dump_json())
            with self.assertRaisesRegex(ValueError, "starting snapshot"):
                self.export(payload, Path(directory) / "wrong-snapshot")

    def test_case_collisions_and_unsupported_runtime_are_rejected(self):
        payload = self.package()
        duplicate = deepcopy(payload)
        duplicate["solution"]["files"].append({**duplicate["solution"]["files"][0], "path": "Warehouse.py"})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Duplicate or case-colliding"):
                self.export(duplicate, directory)
            for language in ("java", "javascript", "typescript"):
                with self.subTest(language=language), self.assertRaisesRegex(ValueError, "Python"):
                    self.export({**payload, "task": {**payload["task"], "language": language}}, directory)

    def test_python_runtime_must_match_static_project_requirement(self):
        payload = self.package(files={"repository/pyproject.toml": '[project]\nrequires-python = ">=3.12"\n'})
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "requires-python"):
            self.export(payload, directory)
