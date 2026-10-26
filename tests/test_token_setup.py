from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "setup_discord_token.py"
MODULE_SPEC = importlib.util.spec_from_file_location("setup_discord_token", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
setup_discord_token = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(setup_discord_token)


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class TokenSetupTests(unittest.TestCase):
    def _project(self, *, ignore: bool = True) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        (root / ".gitignore").write_text(".env.*\n" if ignore else "", encoding="utf-8")
        return directory

    def test_preserves_unrelated_settings_and_writes_private_file(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            target = root / ".env.local"
            target.write_text(
                "# keep this comment\n"
                "DISCORD_ALLOWED_GUILDS=123456789012345678\n"
                "DISCORD_BOT_TOKEN=\n"
                "MAILBOT_DATABASE_PATH=data/custom.sqlite3\n",
                encoding="utf-8",
            )
            os.chmod(target, 0o644)

            setup_discord_token.save_token(root, "synthetic-token")

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "# keep this comment\n"
                "DISCORD_ALLOWED_GUILDS=123456789012345678\n"
                "DISCORD_BOT_TOKEN=synthetic-token\n"
                "MAILBOT_DATABASE_PATH=data/custom.sqlite3\n",
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_existing_configured_token_is_preserved_without_replace(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            target = root / ".env.local"
            original = "DISCORD_BOT_TOKEN=old-synthetic-token\nOTHER=value\n"
            target.write_text(original, encoding="utf-8")
            os.chmod(target, 0o600)

            with self.assertRaisesRegex(
                setup_discord_token.TokenSetupError,
                "already configured",
            ):
                setup_discord_token.save_token(root, "new-synthetic-token")

            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_replace_updates_existing_token_only_when_requested(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            target = root / ".env.local"
            target.write_text(
                "DISCORD_BOT_TOKEN=old-synthetic-token\nOTHER=value\n",
                encoding="utf-8",
            )

            setup_discord_token.save_token(root, "new-synthetic-token", replace=True)

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "DISCORD_BOT_TOKEN=new-synthetic-token\nOTHER=value\n",
            )

    def test_refuses_symlink_destination_without_touching_target(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            real_target = root / "real.env"
            real_target.write_text("OTHER=value\n", encoding="utf-8")
            target = root / ".env.local"
            target.symlink_to(real_target)

            with self.assertRaisesRegex(
                setup_discord_token.TokenSetupError,
                "symlink",
            ):
                setup_discord_token.save_token(root, "synthetic-token")

            self.assertEqual(real_target.read_text(encoding="utf-8"), "OTHER=value\n")
            self.assertTrue(target.is_symlink())

    def test_refuses_tracked_destination(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            target = root / ".env.local"
            target.write_text("OTHER=value\n", encoding="utf-8")
            subprocess.run(["git", "add", "--force", ".env.local"], cwd=root, check=True)

            with self.assertRaisesRegex(
                setup_discord_token.TokenSetupError,
                "tracked",
            ):
                setup_discord_token.save_token(root, "synthetic-token")

            self.assertEqual(target.read_text(encoding="utf-8"), "OTHER=value\n")

    def test_rejects_multiline_and_control_whitespace_without_writing(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            target = root / ".env.local"
            target.write_text("OTHER=value\n", encoding="utf-8")

            invalid_tokens = (
                "line-one\nline-two",
                "line-one\rline-two",
                "line-one\tline-two",
                "line-one\x00line-two",
            )
            for invalid_token in invalid_tokens:
                with self.subTest(invalid_token=repr(invalid_token)):
                    with self.assertRaisesRegex(
                        setup_discord_token.TokenSetupError,
                        "whitespace or control",
                    ):
                        setup_discord_token.save_token(root, invalid_token)
                    self.assertEqual(target.read_text(encoding="utf-8"), "OTHER=value\n")

    def test_rejects_shell_special_characters_without_writing(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            target = root / ".env.local"
            target.write_text("OTHER=value\n", encoding="utf-8")

            invalid_tokens = (
                "token'quote",
                "token$expansion",
                "token`command`",
                "token/slash",
            )
            for invalid_token in invalid_tokens:
                with self.subTest(invalid_token=repr(invalid_token)):
                    with self.assertRaisesRegex(
                        setup_discord_token.TokenSetupError,
                        "unsupported characters",
                    ):
                        setup_discord_token.save_token(root, invalid_token)
                    self.assertEqual(target.read_text(encoding="utf-8"), "OTHER=value\n")

    def test_configure_requires_tty_and_does_not_call_prompt(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            prompted = False

            def prompt(_message: str) -> str:
                nonlocal prompted
                prompted = True
                return "synthetic-token"

            with self.assertRaisesRegex(
                setup_discord_token.TokenSetupError,
                "interactive terminal",
            ):
                setup_discord_token.configure_token(
                    root,
                    stdin=io.StringIO(),
                    stderr=io.StringIO(),
                    prompt_reader=prompt,
                )

            self.assertFalse(prompted)
            self.assertFalse((root / ".env.local").exists())

    def test_configure_success_does_not_echo_token(self) -> None:
        with self._project() as directory:
            root = Path(directory)
            stderr = TtyStringIO()

            setup_discord_token.configure_token(
                root,
                stdin=TtyStringIO(),
                stderr=stderr,
                prompt_reader=lambda _message: "synthetic-token",
            )

            self.assertNotIn("synthetic-token", stderr.getvalue())
            self.assertIn(
                "DISCORD_BOT_TOKEN=synthetic-token\n",
                (root / ".env.local").read_text(encoding="utf-8"),
            )

    def test_getpass_warning_is_rejected_instead_of_echo_fallback(self) -> None:
        with self._project() as directory:
            root = Path(directory)

            def fallback_warning(_message: str) -> str:
                raise setup_discord_token.getpass.GetPassWarning("echo fallback")

            with self.assertRaisesRegex(
                setup_discord_token.TokenSetupError,
                "cancelled",
            ):
                setup_discord_token.configure_token(
                    root,
                    stdin=TtyStringIO(),
                    stderr=TtyStringIO(),
                    prompt_reader=fallback_warning,
                )

            self.assertFalse((root / ".env.local").exists())

    def test_rejects_unignored_destination(self) -> None:
        with self._project(ignore=False) as directory:
            with self.assertRaisesRegex(
                setup_discord_token.TokenSetupError,
                "not ignored",
            ):
                setup_discord_token.save_token(Path(directory), "synthetic-token")


if __name__ == "__main__":
    unittest.main()
