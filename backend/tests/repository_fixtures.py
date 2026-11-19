"""Isolated snapshot directories and mocked GitHub response fixtures."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

GITHUB_COMMIT = "1234567890abcdef1234567890abcdef12345678"


class SnapshotTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.snapshot_directory = Path(temporary.name) / "snapshots"
        patcher = patch.dict(os.environ, {
            "CTF_SNAPSHOT_DIR": str(self.snapshot_directory),
            "CTF_STATE_DIR": str(self.snapshot_directory.parent / "workflow"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)


def github_response(body=b"", *, status=200, headers=None):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.headers = headers or {}
    response.iter_content.return_value = [json.dumps(body).encode() if isinstance(body, dict) else body]
    return response


def github_metadata(repository, branch="main", sha=GITHUB_COMMIT):
    return [
        github_response({"full_name": repository, "default_branch": branch, "private": False}),
        github_response({"name": branch, "commit": {"sha": sha}}),
    ]


def github_archive_responses(repository, archive, branch="main", sha=GITHUB_COMMIT):
    return [
        *github_metadata(repository, branch, sha),
        github_response(
            status=302,
            headers={"Location": f"https://codeload.github.com/{repository}/legacy.zip/{sha.lower()}"},
        ),
        github_response(archive),
    ]
