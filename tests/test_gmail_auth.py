from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "setup_gmail.py"
MODULE_SPEC = importlib.util.spec_from_file_location("setup_gmail", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
setup_gmail = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(setup_gmail)


class GmailAuthSetupTests(unittest.TestCase):
    def _project(self, *, ignored: bool = True) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        (root / ".gitignore").write_text("data/\n" if ignored else "", encoding="utf-8")
        return directory

    def _client(self, root: Path, **installed: object) -> Path:
        source = root / "Downloads" / "client_secret.json"
        source.parent.mkdir()
        payload = {
            "installed": {
                "client_id": "desktop-client-id",
                "client_secret": "desktop-client-secret",
                "auth_uri": setup_gmail.AUTH_URI,
                "token_uri": setup_gmail.TOKEN_URI,
                "redirect_uris": ["http://localhost"],
                **installed,
            }
        }
        source.write_text(json.dumps(payload), encoding="utf-8")
        return source

    def test_client_is_validated_and_copied_without_touching_download(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            source = self._client(root)
            original = source.read_bytes()
            target = setup_gmail._verify_target(root, setup_gmail.CLIENT_RELATIVE)
            setup_gmail._private_parent(root)

            copied = setup_gmail._read_client(source)
            setup_gmail._atomic_write(target, copied)

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / "data").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "data/google").stat().st_mode), 0o700)

    def test_rejects_web_or_unexpected_endpoint_without_writing(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            source = self._client(root, auth_uri="https://attacker.invalid/auth")
            with self.assertRaisesRegex(setup_gmail.GmailSetupError, "endpoint"):
                setup_gmail._read_client(source)
            self.assertFalse((root / "data").exists())

            source.write_text(json.dumps({"web": {"auth_uri": setup_gmail.AUTH_URI}}), encoding="utf-8")
            with self.assertRaisesRegex(setup_gmail.GmailSetupError, "installed desktop"):
                setup_gmail._read_client(source)

    def test_rejects_symlink_tracked_and_unignored_targets(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/google-target").mkdir()
            (root / "data/google").symlink_to(root / "data/google-target", target_is_directory=True)
            with self.assertRaisesRegex(setup_gmail.GmailSetupError, "symlink"):
                setup_gmail._verify_target(root, setup_gmail.TOKEN_RELATIVE)

        with self._project(ignored=False) as directory:
            with self.assertRaisesRegex(setup_gmail.GmailSetupError, "Git-ignored"):
                setup_gmail._verify_target(Path(directory), setup_gmail.TOKEN_RELATIVE)

        with self._project() as directory:
            root = Path(directory)
            target = root / setup_gmail.TOKEN_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_text("tracked", encoding="utf-8")
            subprocess.run(["git", "add", "--force", str(target.relative_to(root))], cwd=root, check=True)
            with self.assertRaisesRegex(setup_gmail.GmailSetupError, "tracked"):
                setup_gmail._verify_target(root, setup_gmail.TOKEN_RELATIVE)

    def test_existing_token_requires_replace_and_is_not_overwritten(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            source = self._client(root)
            token = root / setup_gmail.TOKEN_RELATIVE
            token.parent.mkdir(parents=True)
            token.write_text("old-token", encoding="utf-8")
            with self.assertRaisesRegex(setup_gmail.GmailSetupError, "--replace"):
                setup_gmail.bootstrap(root, source)
            self.assertEqual(token.read_text(encoding="utf-8"), "old-token")

    def test_fake_oauth_uses_only_read_scope_and_safe_console_output(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            source = self._client(root)
            calls: dict[str, object] = {}

            class Credentials:
                def to_json(self) -> str:
                    return json.dumps({"token": "secret-token", "account": "person@example.com", "refresh_token":"synthetic-refresh", "client_id":"synthetic-id", "client_secret":"synthetic-secret", "token_uri":setup_gmail.TOKEN_URI,"scopes":setup_gmail.SCOPES})

            class Flow:
                @classmethod
                def from_client_secrets_file(cls, filename: str, scopes: list[str]) -> "Flow":
                    calls["filename"] = filename
                    calls["scopes"] = scopes
                    return cls()

                def run_local_server(self, **kwargs: object) -> Credentials:
                    calls.update(kwargs)
                    return Credentials()

            package = types.ModuleType("google_auth_oauthlib")
            package.__path__ = []  # type: ignore[attr-defined]
            flow_module = types.ModuleType("google_auth_oauthlib.flow")
            flow_module.InstalledAppFlow = Flow  # type: ignore[attr-defined]
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(
                sys.modules,
                {"google_auth_oauthlib": package, "google_auth_oauthlib.flow": flow_module},
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                client_target, token_target = setup_gmail.bootstrap(root, source)

            self.assertEqual(calls["scopes"], setup_gmail.SCOPES)
            self.assertEqual(calls["access_type"], "offline")
            self.assertEqual(calls["prompt"], "consent")
            self.assertEqual(calls["authorization_prompt_message"], "")
            self.assertEqual(calls["success_message"], setup_gmail.SUCCESS_MESSAGE)
            self.assertEqual(Path(calls["filename"]), client_target)
            self.assertIn(b"secret-token", token_target.read_bytes())
            self.assertNotIn("secret-token", stdout.getvalue() + stderr.getvalue())
            self.assertNotIn("person@example.com", stdout.getvalue() + stderr.getvalue())
            self.assertNotIn("https://accounts.google.com", stdout.getvalue() + stderr.getvalue())
            self.assertEqual(stat.S_IMODE(token_target.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
