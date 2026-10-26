import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from mailbot.__main__ import main
from mailbot.storage import SqliteStore


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "state" / "mailbot.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_tasks_and_credits_survive_restart_and_due_reminder_is_recoverable(self) -> None:
        store = SqliteStore(self.database_path)
        task = store.add_task(
            "再起動後に確認",
            due_at=1,
            task_id="restart-task",
        )
        credit = store.add_game_credit("maimai", 3, note="手動")
        self.assertEqual(task["id"], "restart-task")
        self.assertEqual(credit["credits"], 3)
        store.close()

        reopened = SqliteStore(self.database_path)
        try:
            self.assertEqual(reopened.get_task("restart-task")["title"], "再起動後に確認")
            self.assertEqual(reopened.list_game_credits()[0]["game"], "maimai")
            pending = reopened.pending_reminders(now=2)
            self.assertEqual([row["id"] for row in pending], ["restart-task"])

            self.assertTrue(reopened.mark_reminder_sent("restart-task", sent_at=3))
            self.assertEqual(reopened.pending_reminders(now=4), [])
        finally:
            reopened.close()

        reopened_again = SqliteStore(self.database_path)
        try:
            self.assertEqual(reopened_again.pending_reminders(now=10), [])
            reopened_again.complete_task("restart-task", completed_at=11)
            self.assertTrue(reopened_again.get_task("restart-task")["completed"])
        finally:
            reopened_again.close()

    def test_backup_is_readable_and_does_not_overwrite_without_force(self) -> None:
        store = SqliteStore(self.database_path)
        try:
            store.add_task("backup me", task_id="backup-task")
            backup_path = self.root / "backups" / "state.sqlite3"
            self.assertEqual(store.backup_to(backup_path), backup_path)
            with SqliteStore(backup_path) as backup:
                self.assertEqual(backup.get_task("backup-task")["title"], "backup me")
            with self.assertRaises(FileExistsError):
                store.backup_to(backup_path)
            store.add_task("updated", task_id="updated-task")
            store.backup_to(backup_path, overwrite=True)
        finally:
            store.close()

    def test_cli_demo_is_offline_and_idempotent(self) -> None:
        output_path = self.root / "demo.sqlite3"
        args = ["--db", str(output_path), "demo"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 0)
        with SqliteStore(output_path) as store:
            self.assertEqual(len(store.list_tasks()), 1)
            self.assertEqual(len(store.list_game_credits(game="demo")), 1)
            payload = json.loads(store.export_status_json())
            self.assertEqual(payload["integrations"]["gmail"], "NOT_CONFIGURED")


if __name__ == "__main__":
    unittest.main()
