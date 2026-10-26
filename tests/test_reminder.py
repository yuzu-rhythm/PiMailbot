import asyncio
from pathlib import Path
import tempfile
import unittest

from mailbot.config import Settings
from mailbot.discord_adapter import DiscordAdapter
from mailbot.storage import SqliteStore


class FakeAllowedMentions:
    @staticmethod
    def none() -> object:
        return object()


class FakeDiscord:
    HTTPException = RuntimeError
    AllowedMentions = FakeAllowedMentions


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class FakeChannel:
    def __init__(self, channel_id: int, guild_id: int, *, fail_once: bool = False) -> None:
        self.id = channel_id
        self.guild = FakeGuild(guild_id)
        self.messages: list[str] = []
        self.fail_once = fail_once

    async def send(self, message: str, **_kwargs: object) -> None:
        if self.fail_once:
            self.fail_once = False
            raise FakeDiscord.HTTPException("temporary send failure")
        self.messages.append(message)


class FakeBot:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channel if self.channel.id == channel_id else None


class ReminderWorkerTests(unittest.TestCase):
    def test_due_task_is_sent_and_marked_only_after_channel_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                database_path=root / "state.sqlite3",
                image_dir=root / "images",
                allowed_guild_ids=frozenset({1}),
                allowed_channel_ids=frozenset({2}),
                allowed_user_ids=frozenset({3}),
            )
            with SqliteStore(settings.database_path) as store:
                store.add_task("送信するリマインダー", due_at=1, task_id="reminder-task")
                adapter = DiscordAdapter(store, settings)
                channel = FakeChannel(channel_id=2, guild_id=1)
                bot = FakeBot(channel)
                asyncio.run(adapter._deliver_pending_reminders(bot, FakeDiscord))
                self.assertEqual(channel.messages, ["リマインダー: 送信するリマインダー (due=1)"])
                self.assertEqual(store.pending_reminders(now=2), [])

    def test_wrong_guild_is_not_sent_or_marked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                database_path=root / "state.sqlite3",
                image_dir=root / "images",
                allowed_guild_ids=frozenset({1}),
                allowed_channel_ids=frozenset({2}),
                allowed_user_ids=frozenset({3}),
            )
            with SqliteStore(settings.database_path) as store:
                store.add_task("別guild", due_at=1, task_id="wrong-guild")
                adapter = DiscordAdapter(store, settings)
                channel = FakeChannel(channel_id=2, guild_id=999)
                asyncio.run(adapter._deliver_pending_reminders(FakeBot(channel), FakeDiscord))
                self.assertEqual(channel.messages, [])
                self.assertEqual([row["id"] for row in store.pending_reminders(now=2)], ["wrong-guild"])

    def test_failed_send_is_retryable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "state.sqlite3"
            settings = Settings(
                database_path=database_path,
                image_dir=root / "images",
                allowed_guild_ids=frozenset({1}),
                allowed_channel_ids=frozenset({2}),
                allowed_user_ids=frozenset({3}),
            )
            store = SqliteStore(database_path)
            store.add_task("再起動後に再試行", due_at=1, task_id="retry-task")
            adapter = DiscordAdapter(store, settings)
            first_channel = FakeChannel(channel_id=2, guild_id=1, fail_once=True)
            asyncio.run(adapter._deliver_pending_reminders(FakeBot(first_channel), FakeDiscord))
            self.assertEqual(store.pending_reminders(now=2)[0]["id"], "retry-task")
            store.close()

            reopened = SqliteStore(database_path)
            try:
                retry_channel = FakeChannel(channel_id=2, guild_id=1)
                retry_adapter = DiscordAdapter(reopened, settings)
                asyncio.run(retry_adapter._deliver_pending_reminders(FakeBot(retry_channel), FakeDiscord))
                self.assertEqual(retry_channel.messages, ["リマインダー: 再起動後に再試行 (due=1)"])
                self.assertEqual(reopened.pending_reminders(now=2), [])
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
