import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mailbot.__main__ import main
from mailbot.attachments import AttachmentError, AttachmentStore
from mailbot.config import ConfigurationError, Settings
from mailbot.storage import SqliteStore


PNG = b"\x89PNG\r\n\x1a\n" + b"offline-test"


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_discord_authorization_is_default_deny_and_token_is_not_public(self) -> None:
        secret = "test-token-must-not-be-printed"
        settings = Settings.from_env(
            {
                "DISCORD_BOT_TOKEN": secret,
            }
        )
        self.assertFalse(settings.discord_authorized(guild_id=123, channel_id=456, user_id=789))
        self.assertNotIn(secret, repr(settings))
        self.assertNotIn(secret, str(settings.public_dict()))

        configured = Settings.from_env(
            {
                "DISCORD_BOT_TOKEN": secret,
                "DISCORD_ALLOWED_GUILDS": "123",
                "DISCORD_ALLOWED_CHANNELS": "456",
                "DISCORD_ALLOWED_USERS": "789",
            }
        )
        self.assertTrue(configured.discord_authorized(guild_id=123, channel_id=456, user_id=789))
        self.assertFalse(configured.discord_authorized(guild_id=123, channel_id=456, user_id=790))
        self.assertFalse(configured.discord_authorized(guild_id=None, channel_id=456, user_id=789))

    def test_invalid_discord_id_configuration_fails_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"DISCORD_ALLOWED_GUILDS": "123,nope"})
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"MAILBOT_MAX_IMAGE_BYTES": "0"})

    def test_image_path_is_bounded_hashed_and_deduplicated(self) -> None:
        database_path = self.root / "state.sqlite3"
        image_root = self.root / "images"
        with SqliteStore(database_path) as store:
            attachments = AttachmentStore(image_root, store, max_bytes=1024)
            first = attachments.save_image("../../private/../receipt.png", PNG)
            second = attachments.save_image("different-name.png", PNG)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["stored_path"], second["stored_path"])
            stored = Path(first["stored_path"])
            self.assertEqual(stored.parent.resolve(), image_root.resolve())
            self.assertNotIn("/", first["stored_name"])
            self.assertNotIn("\\", first["stored_name"])
            self.assertEqual(stored.read_bytes(), PNG)
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(store.list_images()), 1)

    def test_image_size_and_type_limits_are_enforced(self) -> None:
        with SqliteStore(self.root / "state.sqlite3") as store:
            attachments = AttachmentStore(self.root / "images", store, max_bytes=len(PNG))
            with self.assertRaises(AttachmentError):
                attachments.save_image("large.png", PNG + b"too large")
            with self.assertRaises(AttachmentError):
                attachments.save_image("fake.png", b"not an image")
            with self.assertRaises(AttachmentError):
                attachments.save_image("wrong-type.png", PNG, content_type="image/jpeg")

    def test_status_cli_does_not_print_discord_token(self) -> None:
        secret = "status-secret-that-must-not-appear"
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": secret,
                "DISCORD_ALLOWED_GUILDS": "",
                "DISCORD_ALLOWED_CHANNELS": "",
                "DISCORD_ALLOWED_USERS": "",
            },
            clear=False,
        ), contextlib.redirect_stdout(stdout):
            result = main(["--db", str(self.root / "status.sqlite3"), "status"])
        self.assertEqual(result, 0)
        self.assertNotIn(secret, stdout.getvalue())
        self.assertIn("NOT_CONFIGURED", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
