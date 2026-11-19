import hashlib
import io
import json
import os
import struct
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient

from app.main import app
from app.services import repo_service, snapshot_service
from tests.repository_fixtures import GITHUB_COMMIT, SnapshotTestCase, github_metadata, github_response
from tests.test_contextual_task_endpoints import MOCK_TASK_PAYLOAD, SAMPLE_REPO_ANALYSIS
from tests.test_validation_endpoint import SAMPLE_SOLUTION, SAMPLE_TASK, SAMPLE_TEST_SUITE

client = TestClient(app)


def zip_bytes(files, *, comment=b"", compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        archive.comment = comment
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def upload(data, filename="repo.zip"):
    return client.post("/tasks/analyze/upload", files={"file": (filename, data, "application/zip")})


class TestLocalSnapshots(SnapshotTestCase):
    def test_uploaded_bytes_are_reused_and_restore_as_independent_workspaces(self):
        files = {
            "repo/README.md": b"Version one\r\n",
            "repo/data/blob.bin": b"\x00\xff\x83",
            "repo/tests/test_math.py": b"import unittest\n",
            "repo/requirements.txt": b"fastapi\n",
            "repo/.git/HEAD": GITHUB_COMMIT.encode(),
        }
        original = zip_bytes(files, comment=GITHUB_COMMIT.encode())
        response = upload(original)
        self.assertEqual(response.status_code, 200, response.text)
        analysis = response.json()
        digest = hashlib.sha256(original).hexdigest()
        snapshot_id = analysis["snapshot_id"]
        self.assertEqual(analysis["content_sha256"], digest)
        self.assertEqual(analysis["repository_identity"], f"zip:sha256:{digest}")
        self.assertIsNone(analysis["commit_sha"])
        self.assertEqual(analysis["existing_test_files"], ["tests/test_math.py"])
        self.assertEqual(analysis["baseline_test_commands"], [{
            "framework": "unittest", "working_directory": ".",
            "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
        }])
        self.assertEqual(upload(original, "renamed.zip").json()["snapshot_id"], snapshot_id)

        record = snapshot_service.load_snapshot(snapshot_id)
        self.assertEqual((self.snapshot_directory / record.archive_location).read_bytes(), original)
        with snapshot_service.restore_snapshot(snapshot_id) as (first_root, first_record):
            self.assertEqual(first_record, record)
            for name, content in files.items():
                self.assertEqual((first_root / name.removeprefix("repo/")).read_bytes(), content)
            (first_root / "README.md").write_bytes(b"changed in disposable copy")
            with snapshot_service.restore_snapshot(snapshot_id) as (second_root, _):
                self.assertNotEqual(first_root, second_root)
                self.assertEqual((second_root / "README.md").read_bytes(), files["repo/README.md"])
        self.assertFalse(first_root.exists())
        self.assertNotEqual(upload(zip_bytes({"README.md": "Version two"})).json()["snapshot_id"], snapshot_id)

    def test_parallel_duplicates_publish_one_complete_record(self):
        data = zip_bytes({"README.md": "concurrent fixture"})

        def ingest():
            return repo_service.analyze_uploaded_zip(io.BytesIO(data), "repo.zip")

        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = list(workers.map(lambda _: ingest(), range(2)))
        self.assertEqual(first, second)
        self.assertEqual([entry.name for entry in self.snapshot_directory.iterdir()], [first.snapshot_id])
        self.assertEqual(snapshot_service.load_snapshot(first.snapshot_id).analysis, first)

    def test_records_commands_with_project_relative_working_directories(self):
        data = zip_bytes({
            "repository/backend/pytest.ini": "[pytest]\n",
            "repository/backend/tests/test_api.py": "def test_api(): pass\n",
            "repository/web/package.json": '{"scripts":{"test":"vitest run"},"packageManager":"npm@10"}',
            "repository/web/src/widget.test.ts": "test('widget', () => {})",
            "repository/tool/go.mod": "module example.com/tool\n",
            "repository/tool/app_test.go": "package tool\n",
            "repository/unknown/spec.test.js": "test('unknown runner', () => {})",
        })
        response = upload(data)
        self.assertEqual(response.status_code, 200, response.text)
        analysis = response.json()
        self.assertEqual(analysis["test_frameworks"], ["go test", "pytest", "vitest"])
        self.assertEqual(
            analysis["baseline_test_commands"],
            [
                {"framework": "go test", "working_directory": "tool", "argv": ["go", "test", "./..."]},
                {"framework": "pytest", "working_directory": "backend", "argv": ["python", "-m", "pytest"]},
                {"framework": "vitest", "working_directory": "web", "argv": ["npm", "test"]},
            ],
        )

    def test_replacement_with_another_valid_zip_is_detected(self):
        snapshot_id = upload(zip_bytes({"source.py": "original"})).json()["snapshot_id"]
        archive = self.snapshot_directory / snapshot_id / "source.zip"
        archive.write_bytes(zip_bytes({"source.py": "modified"}))
        with self.assertRaisesRegex(snapshot_service.SnapshotIntegrityError, "checksum"):
            snapshot_service.load_snapshot(snapshot_id)
        with self.assertRaises(snapshot_service.SnapshotIntegrityError):
            with snapshot_service.restore_snapshot(snapshot_id):
                self.fail("A changed archive must never yield a workspace")
        response = client.post("/tasks/generate/from-repo", json={"snapshot_id": snapshot_id})
        self.assertEqual(response.status_code, 409)

    def test_rewriting_archive_and_manifest_digest_cannot_rebind_the_id(self):
        snapshot_id = upload(zip_bytes({"source.py": "original"})).json()["snapshot_id"]
        entry = self.snapshot_directory / snapshot_id
        changed = zip_bytes({"source.py": "replacement"})
        digest = hashlib.sha256(changed).hexdigest()
        (entry / "source.zip").write_bytes(changed)
        metadata = entry / "metadata.json"
        payload = json.loads(metadata.read_bytes())
        payload["analysis"].update(content_sha256=digest, repository_identity=f"zip:sha256:{digest}")
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(snapshot_service.SnapshotIntegrityError, "does not match its ID"):
            snapshot_service.load_snapshot(snapshot_id)

    def test_link_to_matching_bytes_is_rejected(self):
        data = zip_bytes({"source.py": "original"})
        snapshot_id = upload(data).json()["snapshot_id"]
        archive = self.snapshot_directory / snapshot_id / "source.zip"
        outside = self.snapshot_directory.parent / "outside.zip"
        outside.write_bytes(data)
        archive.unlink()
        # A hardlink avoids requiring symlink privileges on native Windows.
        os.link(outside, archive)
        with self.assertRaisesRegex(snapshot_service.SnapshotIntegrityError, "unsafe file or link"):
            snapshot_service.load_snapshot(snapshot_id)
        self.assertEqual(outside.read_bytes(), data)

    def test_missing_and_malformed_snapshot_references_are_rejected(self):
        with self.assertRaises(snapshot_service.SnapshotError):
            snapshot_service.load_snapshot("../outside")
        missing = "snap_" + "a" * 64
        self.assertEqual(client.post("/tasks/generate/from-repo", json={"snapshot_id": missing}).status_code, 404)
        self.assertEqual(client.post("/tasks/solution", json={"task": {**SAMPLE_TASK, "snapshot_id": missing}}).status_code, 404)
        self.assertEqual(client.post("/tasks/generate/from-repo", json={"snapshot_id": "../outside"}).status_code, 422)

    def test_task_generation_uses_stored_analysis_and_ignores_legacy_sha_claim(self):
        analysis = upload(zip_bytes({"source.py": "repository source"})).json()
        forged = {**analysis, "project_name": "forged", "commit_sha": GITHUB_COMMIT, "content_sha256": "b" * 64}
        with patch("app.services.task_service._get_gemini_client") as provider:
            provider.return_value.models.generate_content.return_value.text = json.dumps(MOCK_TASK_PAYLOAD)
            anchored = client.post("/tasks/generate/from-repo", json={"repo_analysis": forged})
            legacy = client.post("/tasks/generate/from-repo", json={
                "repo_analysis": {**SAMPLE_REPO_ANALYSIS, "commit_sha": GITHUB_COMMIT}
            })
        self.assertEqual(anchored.status_code, 201, anchored.text)
        self.assertEqual(anchored.json()["project_name"], analysis["project_name"])
        self.assertEqual(anchored.json()["snapshot_id"], analysis["snapshot_id"])
        self.assertIsNone(anchored.json()["starting_commit_sha"])
        self.assertEqual(legacy.status_code, 201)
        self.assertIsNone(legacy.json()["snapshot_id"])
        self.assertIsNone(legacy.json()["starting_commit_sha"])

    def test_validation_checks_real_source_and_rejects_conflicting_references(self):
        snapshot_id = upload(zip_bytes({"README.md": "missing app files"})).json()["snapshot_id"]
        other = upload(zip_bytes({"README.md": "different snapshot"})).json()["snapshot_id"]
        payload = {
            "task": {**SAMPLE_TASK, "snapshot_id": snapshot_id},
            "solution": {**SAMPLE_SOLUTION, "snapshot_id": snapshot_id},
            "test_suite": SAMPLE_TEST_SUITE,
        }
        result = client.post("/tasks/validate", json=payload)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["overall_verdict"], "REJECTED")
        self.assertIsNone(result.json()["starting_commit_sha"])
        self.assertIn("does not exist", str(result.json()["rejection_reasons"]))
        conflicting = deepcopy(payload)
        conflicting["solution"]["snapshot_id"] = other
        self.assertEqual(client.post("/tasks/validate", json=conflicting).status_code, 400)
        self.assertEqual(client.post("/tasks/export/harbor", json=conflicting).status_code, 400)
        with snapshot_service.restore_snapshot(snapshot_id) as (root, _):
            self.assertFalse((root / "app" / "cache.py").exists())

    def test_corrupt_zip_is_rejected_without_publishing(self):
        broken = bytearray(zip_bytes({"file.txt": "original"}))
        name_length, extra_length = struct.unpack_from("<HH", broken, 26)
        broken[30 + name_length + extra_length] ^= 0xff
        response = upload(bytes(broken))
        self.assertEqual(response.status_code, 400)
        self.assertIn("corrupt", response.json()["detail"])
        self.assertFalse(self.snapshot_directory.exists())

    def test_windows_aliases_and_conflicting_paths_are_rejected(self):
        for paths in (
            {"a/COM1.txt": "invalid"},
            {"a/file.txt:stream": "invalid"},
            {"a/child. /x": "invalid"},
            {"Lib/x.py": "one", "lib/y.py": "two"},
            {"f": "regular file", "f/nested.py": "conflict"},
        ):
            with self.subTest(paths=paths):
                self.assertEqual(upload(zip_bytes(paths)).status_code, 400)
        self.assertFalse(self.snapshot_directory.exists())

    def test_zip_bomb_is_rejected_before_opening_any_entry(self):
        data = zip_bytes({"large.txt": b"a" * 100_000}, compression=zipfile.ZIP_DEFLATED)
        with patch("app.services.archive_service.zipfile.ZipFile.open", side_effect=AssertionError):
            response = upload(data)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("compression ratio", response.json()["detail"])


class TestGitHubSnapshotFailures(SnapshotTestCase):
    def _github_analysis(self, responses, **options):
        with (
            patch("app.services.repo_service._verify_hostname_and_dns", return_value="140.82.112.6"),
            patch("app.services.repo_service.requests.Session.get", side_effect=responses) as request,
        ):
            result = client.post("/tasks/analyze/github", json={"repo_url": "https://github.com/owner/repo", **options})
        return result, request.call_count

    def test_explicit_revision_is_verified_even_if_the_default_branch_has_advanced(self):
        result, count = self._github_analysis([
            github_metadata("owner/repo", branch="new-default")[0],
            github_response({"sha": GITHUB_COMMIT}),
            github_response(zip_bytes({"source.py": "version"})),
        ], commit_sha=GITHUB_COMMIT.upper())
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(count, 3)
        self.assertEqual(result.json()["commit_sha"], GITHUB_COMMIT)
        refused, count = self._github_analysis([
            github_metadata("owner/repo")[0], github_response({"sha": "e" * 40}),
        ], commit_sha=GITHUB_COMMIT)
        self.assertEqual(refused.status_code, 502)
        self.assertEqual(count, 2)

    def test_explicit_revision_rejects_moving_refs_and_short_hashes(self):
        for reference in ("main", "ed47eab"):
            result, count = self._github_analysis([], commit_sha=reference)
            self.assertEqual(result.status_code, 422)
            self.assertEqual(count, 0)

    def test_a_branch_name_cannot_replace_the_sha_in_an_archive_redirect(self):
        result, request_count = self._github_analysis([
            *github_metadata("owner/repo"),
            github_response(status=302, headers={"Location": "https://codeload.github.com/owner/repo/zip/main"}),
        ])
        self.assertEqual(result.status_code, 400)
        self.assertEqual(request_count, 3)
        self.assertFalse(self.snapshot_directory.exists())

    def test_same_host_redirects_require_https_and_default_port(self):
        for origin in ("http://codeload.github.com", "https://codeload.github.com:444", "https://user@codeload.github.com"):
            with self.subTest(origin=origin):
                result, request_count = self._github_analysis([
                    *github_metadata("owner/repo"),
                    github_response(status=302, headers={"Location": f"{origin}/owner/repo/zip/{GITHUB_COMMIT}"}),
                ])
                self.assertEqual(result.status_code, 400)
                self.assertEqual(request_count, 3)

    def test_github_rejects_empty_repo_throttling_and_invalid_commit_metadata(self):
        for responses, expected in (
            ([github_response(status=403, headers={"X-RateLimit-Remaining": "0"})], 429),
            ([github_response(status=429)], 429),
            ([github_metadata("owner/repo")[0], github_response(status=404)], 400),
            (github_metadata("owner/repo", sha="deadbeef"), 502),
            ([*github_metadata("owner/repo"), github_response(b"truncated archive")], 502),
            ([github_response(status=503)], 502),
            ([requests.Timeout()], 502),
        ):
            with self.subTest(status=expected):
                result, request_count = self._github_analysis(responses)
                self.assertEqual(result.status_code, expected, result.text)
                self.assertEqual(request_count, len(responses))
        self.assertFalse(self.snapshot_directory.exists())

    def test_github_archive_comment_must_agree_when_it_contains_a_sha(self):
        archive = zip_bytes({"source.py": "fixture"}, comment=b"e" * 40)
        result, _ = self._github_analysis([*github_metadata("owner/repo"), github_response(archive)])
        self.assertEqual(result.status_code, 502)
        self.assertFalse(self.snapshot_directory.exists())

    def test_adapter_pins_connection_ip_and_preserves_certificate_hostname(self):
        host = "api.github.com"
        adapter = repo_service._PinnedHTTPSAdapter(host, "140.82.112.6")
        self.addCleanup(adapter.close)
        request = requests.Request("GET", f"https://{host}/repos/owner/repo").prepare()
        pool = adapter.get_connection_with_tls_context(request, verify=True)
        self.assertEqual(pool.host, "140.82.112.6")
        self.assertEqual(pool.assert_hostname, host)
        self.assertEqual(pool.conn_kw["server_hostname"], host)
