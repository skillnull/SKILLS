from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCANNER = PROJECT_ROOT / "scripts/check_private_content.py"


class PrivateContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test Maintainer")
        self.git("config", "user.email", "maintainer@example.com")
        self.environment = dict(os.environ, PRIVATE_CONTENT_TERMS_JSON=json.dumps(["private-fixture"]))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments], cwd=self.repo, text=True, capture_output=True,
            check=True, env=getattr(self, "environment", None),
        )

    def write(self, name: str, text: str) -> Path:
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def scan(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), *arguments], cwd=self.repo,
            env=self.environment, text=True, capture_output=True,
        )

    def test_scans_staged_bytes_instead_of_working_copy(self) -> None:
        self.write("notes.md", "private-fixture\n")
        self.git("add", "notes.md")
        self.write("notes.md", "generic notes\n")
        result = self.scan("--staged", "--require-private-terms")
        self.assertEqual(result.returncode, 1)
        self.assertIn("private-term", result.stderr)
        self.assertNotIn("private-fixture", result.stderr)
        self.assertEqual(self.scan().returncode, 0)

    def test_detects_private_content_removed_from_current_tree(self) -> None:
        self.write("notes.md", "private-fixture\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial notes")
        self.write("notes.md", "generic notes\n")
        self.git("add", ".")
        self.git("commit", "-qm", "generalize notes")
        self.assertEqual(self.scan().returncode, 0)
        result = self.scan("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("history:", result.stderr)

    def test_scans_pending_and_historical_messages(self) -> None:
        message = self.write(".git/pending-message", "private-fixture\n")
        result = self.scan("--message-file", str(message))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("private-fixture", result.stderr)
        self.write("notes.md", "generic notes\n")
        self.git("add", ".")
        self.git("commit", "-qm", "private-fixture")
        self.assertIn("commit:", self.scan("--history").stderr)

    def test_redacts_private_filenames(self) -> None:
        self.write("private-fixture.md", "generic notes\n")
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("[redacted-path]", result.stderr)
        self.assertNotIn("private-fixture", result.stderr)

    def test_ignored_terms_are_loaded_but_not_published(self) -> None:
        self.environment.pop("PRIVATE_CONTENT_TERMS_JSON")
        self.write(".gitignore", ".privacy.local.json\n")
        terms_file = self.write(".privacy.local.json", json.dumps(["private-fixture"]))
        terms_file.chmod(0o600)
        self.write("notes.md", "generic notes\n")
        self.assertEqual(self.scan("--require-private-terms").returncode, 0)
        self.git("add", "-f", ".privacy.local.json")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertIn("local-only-file", result.stderr)

    def test_private_terms_file_requires_private_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX file permissions are not available on Windows.")
        self.environment.pop("PRIVATE_CONTENT_TERMS_JSON")
        terms_file = self.write(".privacy.local.json", json.dumps(["private-fixture"]))
        terms_file.chmod(0o644)
        result = self.scan("--require-private-terms")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not grant group or other access", result.stderr)
        self.assertNotIn("private-fixture", result.stderr)

    def test_private_terms_file_rejects_dangling_symlinks(self) -> None:
        if os.name == "nt":
            self.skipTest("Symbolic-link behavior differs on Windows.")
        self.environment.pop("PRIVATE_CONTENT_TERMS_JSON")
        (self.repo / ".privacy.local.json").symlink_to("missing-private-terms.json")
        result = self.scan("--require-private-terms")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be a regular file", result.stderr)

    def test_json_output_is_structured_and_redacted(self) -> None:
        self.write("notes.md", "private-fixture\n")
        result = self.scan("--format", "json")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["exit_code"], 1)
        self.assertTrue(payload["private_terms_configured"])
        self.assertIn("private-term", payload["failures"][0])
        self.assertNotIn("private-fixture", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_missing_empty_and_invalid_terms_block_required_scan(self) -> None:
        for value in ("", "[]", "{invalid private-fixture", '[""]', '{"terms": []}'):
            with self.subTest(value=value):
                self.environment["PRIVATE_CONTENT_TERMS_JSON"] = value
                result = self.scan("--require-private-terms")
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("private-fixture", result.stderr)

    def test_generic_patterns_block_without_echoing_values(self) -> None:
        samples = {
            "personal-path": "/".join(["", "home", "example-user", "notes"]),
            "private-network": ".".join(["192", "168", "1", "7"]),
            "private-key": "-----BEGIN " + "PRIVATE KEY-----",
            "access-token": "ghp_" + "x" * 36,
            "credential-literal": 'password = "' + "x" * 24 + '"',
        }
        for category, text in samples.items():
            with self.subTest(category=category):
                self.write("notes.md", text)
                result = self.scan()
                self.assertEqual(result.returncode, 1)
                self.assertIn(category, result.stderr)
                self.assertNotIn(text, result.stderr)

    def test_binary_and_symlink_files_are_not_silently_skipped(self) -> None:
        binary = self.repo / "payload.bin"
        binary.write_bytes(b"\xff\x00")
        self.assertIn("binary-file-needs-review", self.scan().stderr)
        binary.unlink()
        (self.repo / "linked.md").symlink_to("missing.md")
        self.assertIn("unsupported-file-mode", self.scan().stderr)

    def test_private_ref_and_tag_messages_are_checked(self) -> None:
        self.write("notes.md", "generic notes\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial notes")
        self.git("branch", "private-fixture")
        self.git("tag", "-a", "release", "-m", "private-fixture")
        result = self.scan("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ref-name", result.stderr)
        self.assertIn("tag:", result.stderr)
        self.assertNotIn("private-fixture", result.stderr)

    def test_shallow_history_is_rejected(self) -> None:
        self.write("notes.md", "generic notes\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial notes")
        shallow = self.repo / "shallow-copy"
        subprocess.run(
            ["git", "clone", "-q", "--depth=1", self.repo.as_uri(), str(shallow)],
            check=True, capture_output=True,
        )
        result = subprocess.run(
            [sys.executable, str(SCANNER), "--history"], cwd=shallow,
            env=self.environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Shallow history", result.stderr)

    def test_real_commit_hooks_block_files_and_messages(self) -> None:
        self.write("scripts/check_private_content.py", SCANNER.read_text(encoding="utf-8"))
        self.write(
            "scripts/validate_project.py",
            "#!/usr/bin/env python3\nraise SystemExit(0)\n",
        )
        shutil.copytree(PROJECT_ROOT / ".githooks", self.repo / ".githooks")
        self.git("config", "core.hooksPath", ".githooks")
        self.write("notes.md", "private-fixture\n")
        self.git("add", ".")
        command = ["git", "-c", "core.hooksPath=.githooks", "commit", "-qm"]
        result = subprocess.run(command + ["generic message"], cwd=self.repo, env=self.environment, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.write("notes.md", "generic notes\n")
        self.git("add", "notes.md")
        result = subprocess.run(command + ["private-fixture"], cwd=self.repo, env=self.environment, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        result = subprocess.run(command + ["generic message"], cwd=self.repo, env=self.environment, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
