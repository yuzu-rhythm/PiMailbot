import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mailbot.mail_workflow import (
    AuthorizationError,
    DuplicateMessageConflict,
    MailWorkflowStore,
    StableMessageIdRequired,
    UnknownDelivery,
    WorkflowStorageError,
)


EML = (
    b"Message-ID: <fixture-001@example.invalid>\n"
    b"From: sender@example.invalid\n"
    b"To: recipient@example.invalid\n"
    b"Subject: Offline fixture\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: text/plain; charset=utf-8\n"
    b"\n"
    b"This body is an extractive offline preview, not an LLM summary.\n"
)


class MailWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "private" / "mail.sqlite3"
        self.eml_path = self.root / "message.eml"
        self.eml_path.write_bytes(EML)
        self.policy = {
            "allowed_guild_ids": {100},
            "allowed_channel_ids": {200},
            "allowed_user_ids": {300},
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def open_store(self) -> MailWorkflowStore:
        return MailWorkflowStore(self.database_path, **self.policy)

    def test_ingest_is_normalized_deduplicated_and_restart_safe(self) -> None:
        with self.open_store() as store:
            first = store.ingest_eml(self.eml_path, message_id="demo-001")
            self.assertFalse(first["deduplicated"])
            self.assertEqual(first["identity_key"], "synthetic:demo-001")
            self.assertEqual(first["preview_mode"], "OFFLINE_EXTRACTIVE_PREVIEW")
            self.assertEqual(first["llm_status"], "NOT_CONFIGURED")
            self.assertEqual(len(store.list_messages()), 1)
            self.assertEqual(len(store.list_notifications()), 1)
            self.assertEqual(store.list_notifications()[0]["status"], "PENDING")
            if os.name != "nt":
                for suffix in ("", "-wal", "-shm"):
                    sidecar = Path(f"{self.database_path}{suffix}")
                    if sidecar.exists():
                        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

        with self.open_store() as reopened:
            duplicate = reopened.ingest_eml(self.eml_path, message_id="demo-001")
            self.assertTrue(duplicate["deduplicated"])
            self.assertEqual(len(reopened.list_messages()), 1)
            self.assertEqual(len(reopened.list_notifications()), 1)
            self.assertEqual(reopened.get_message("demo-001")["message_id_kind"], "synthetic")
            with self.assertRaises(DuplicateMessageConflict):
                changed = self.root / "changed.eml"
                changed.write_bytes(EML.replace(b"fixture", b"changed"))
                reopened.ingest_eml(changed, message_id="demo-001")

        if os.name != "nt":
            self.assertEqual(self.database_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.database_path.parent.stat().st_mode & 0o777, 0o700)

    def test_render_and_simulated_delivery_update_outbox_deterministically(self) -> None:
        with self.open_store() as store:
            store.ingest_eml(self.eml_path, message_id="demo-001")
            rendered = store.render_notification("demo-001")
            self.assertIn("OFFLINE_EXTRACTIVE_PREVIEW", rendered["rendered_text"])
            self.assertIn("LLM=NOT_CONFIGURED", rendered["rendered_text"])
            self.assertEqual(rendered["delivery_mode"], "SIMULATED")
            self.assertEqual(rendered["provider_status"], "NOT_CONFIGURED")

            first = store.simulate_delivery("demo-001", 100, 200, user_id=300)
            second = store.simulate_delivery("demo-001", 100, 200, user_id=300)
            self.assertEqual(first["delivery_id"], second["delivery_id"])
            self.assertFalse(first["deduplicated"])
            self.assertTrue(second["deduplicated"])
            self.assertEqual(first["delivery_mode"], "SIMULATED")
            self.assertEqual(store.list_notifications()[0]["status"], "DELIVERED")
            self.assertEqual(store.status()["simulated_delivery_count"], 1)

    def test_reactions_are_authorized_idempotent_and_support_remove_readd(self) -> None:
        with self.open_store() as store:
            store.ingest_eml(self.eml_path, message_id="demo-001")
            delivery = store.simulate_delivery("demo-001", 100, 200)
            delivery_id = str(delivery["delivery_id"])

            first = store.react(
                delivery_id,
                "✅",
                user_id=300,
                guild_id=100,
                channel_id=200,
            )
            duplicate = store.react(
                delivery_id,
                "✅",
                user_id=300,
                guild_id=100,
                channel_id=200,
            )
            self.assertEqual(first["status"], "PENDING")
            self.assertEqual(duplicate["status"], "DUPLICATE")
            self.assertEqual(len(store.list_actions(status="PENDING")), 1)
            self.assertEqual(first["action"]["provider_status"], "NOT_CONFIGURED")
            self.assertEqual(first["action"]["action_kind"], "mark_read")

            removed = store.react(
                delivery_id,
                "✅",
                user_id=300,
                guild_id=100,
                channel_id=200,
                added=False,
            )
            self.assertEqual(removed["status"], "REMOVED")
            self.assertEqual(removed["action"]["status"], "CANCELLED")
            self.assertEqual(len(store.list_actions(status="PENDING")), 0)
            self.assertEqual(
                store.react(
                    delivery_id,
                    "✅",
                    user_id=300,
                    guild_id=100,
                    channel_id=200,
                    added=False,
                )["status"],
                "DUPLICATE",
            )

            readded = store.react(
                delivery_id,
                "✅",
                user_id=300,
                guild_id=100,
                channel_id=200,
                added=True,
            )
            self.assertEqual(readded["status"], "PENDING")
            self.assertEqual(readded["action"]["generation"], 2)
            self.assertEqual(len(store.list_actions()), 2)
            self.assertEqual(store.status()["pending_action_count"], 1)
            self.assertEqual(store.status()["cancelled_action_count"], 1)

    def test_authorization_rejects_unknown_targets_users_and_deliveries(self) -> None:
        with self.open_store() as store:
            store.ingest_eml(self.eml_path, message_id="demo-001")
            with self.assertRaises(AuthorizationError):
                store.simulate_delivery("demo-001", 999, 200)
            with self.assertRaises(AuthorizationError):
                store.simulate_delivery("demo-001", 100, 200, user_id=999)
            delivery = store.simulate_delivery("demo-001", 100, 200)
            with self.assertRaises(AuthorizationError):
                store.react(
                    str(delivery["delivery_id"]),
                    "⭐",
                    user_id=999,
                    guild_id=100,
                    channel_id=200,
                )
            with self.assertRaises(AuthorizationError):
                store.react(
                    str(delivery["delivery_id"]),
                    "⭐",
                    user_id=300,
                    guild_id=100,
                    channel_id=999,
                )
            with self.assertRaises(UnknownDelivery):
                store.react(
                    "simulated-discord:missing",
                    "⭐",
                    user_id=300,
                    guild_id=100,
                    channel_id=200,
                )

    def test_stable_id_bounded_input_and_external_namespace(self) -> None:
        no_id = self.root / "no-id.eml"
        no_id.write_bytes(EML.replace(b"Message-ID: <fixture-001@example.invalid>\n", b""))
        with self.open_store() as store:
            with self.assertRaises(StableMessageIdRequired):
                store.ingest_eml(no_id)
            external = store.ingest_eml(
                EML,
                source_id="provider-message-001",
                message_id_kind="external",
            )
            self.assertEqual(external["identity_key"], "external:provider-message-001")
            with self.assertRaises(ValueError):
                store.ingest_eml(EML, source_id="too-large", max_bytes=1)

    def test_newer_schema_is_rejected(self) -> None:
        self.database_path.parent.mkdir(mode=0o700)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "CREATE TABLE mail_workflow_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO mail_workflow_schema_migrations VALUES (999, 0)"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(WorkflowStorageError):
            self.open_store()


if __name__ == "__main__":
    unittest.main()
