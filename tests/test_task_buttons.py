import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import discord
except ImportError:  # pragma: no cover - optional dependency is skipped below
    discord = None  # type: ignore[assignment]

from mailbot.config import Settings
from mailbot.discord_adapter import DiscordAdapter, _mark_task_message_completed
from mailbot.storage import SqliteStore


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.edited: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.sent.append((args, kwargs))

    async def edit_message(self, *args: object, **kwargs: object) -> None:
        self.edited.append((args, kwargs))


class FakeInteraction:
    def __init__(self, *, user_id: int = 789) -> None:
        self.guild_id = 123
        self.channel_id = 456
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeResponse()
        self.message = SimpleNamespace(content="")


def button_item(button):
    return getattr(button, 'item', button)


@unittest.skipUnless(discord is not None, "discord.py optional dependency is not installed")
class TaskButtonTests(unittest.TestCase):
    def test_repeated_numbered_click_does_not_change_duplicate_title_row(self) -> None:
        content = "タスク一覧 (1/1)\n1. 同じ名前\n   状態: 未完了 / 期限: 期限なし\n2. 同じ名前\n   状態: 完了 / 期限: 期限なし"
        button = SimpleNamespace(label="完了済み 2: 同じ名前")
        self.assertEqual(_mark_task_message_completed(content, {"title": "同じ名前"}, button=button), content)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SqliteStore(self.root / "tasks.sqlite3")
        self.settings = Settings(
            database_path=self.root / "tasks.sqlite3",
            image_dir=self.root / "images",
            discord_token="synthetic-token",
            allowed_guild_ids=frozenset({123}),
            allowed_channel_ids=frozenset({456}),
            allowed_user_ids=frozenset({789}),
        )
        self.adapter = DiscordAdapter(self.store, self.settings)
        self.bot = self.adapter.build_bot()

    def tearDown(self) -> None:
        asyncio.run(self.bot.close())
        self.store.close()
        self.tempdir.cleanup()

    def test_tasks_render_names_and_japan_time_without_internal_ids(self) -> None:
        task = self.store.add_task(
            "研究発表の資料を確認する",
            task_id="internal-long-uuid-value-1234567890",
            due_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )
        interaction = FakeInteraction()

        async def run() -> None:
            command = self.bot.tree.get_command("tasks")
            assert command is not None
            await command.callback(interaction, page=1)

        asyncio.run(run())
        message = str(interaction.response.sent[0][0][0])
        view = interaction.response.sent[0][1]["view"]
        self.assertIn("研究発表の資料を確認する", message)
        self.assertIn("2026/10/24 09:00 (日本時間)", message)
        self.assertNotIn(str(task["id"]), message)
        self.assertLessEqual(len(message), 1900)
        self.assertEqual(len(view.children), 1)
        button = view.children[0]
        self.assertIn(str(task["id"]), button.custom_id)
        self.assertNotIn(str(task["id"]), button_item(button).label)

    def test_authorized_button_completes_and_disables_idempotently(self) -> None:
        self.store.add_task("返信する", task_id="reply-task")
        list_interaction = FakeInteraction()

        async def run() -> None:
            command = self.bot.tree.get_command("tasks")
            assert command is not None
            await command.callback(list_interaction, page=1)

        asyncio.run(run())
        view = list_interaction.response.sent[0][1]["view"]
        button = view.children[0]

        first = FakeInteraction()
        second = FakeInteraction()

        async def click() -> None:
            await button.callback(first)
            await button.callback(second)

        asyncio.run(click())
        task = self.store.get_task("reply-task")
        self.assertTrue(task["completed"])
        self.assertTrue(button_item(button).disabled)
        self.assertIn("完了済み", button_item(button).label)
        self.assertIn("状態: 完了", first.response.edited[0][1]["content"])
        self.assertTrue(second.response.edited)

    def test_unauthorized_button_does_not_touch_store(self) -> None:
        self.store.add_task("秘密のタスク", task_id="private-task")
        list_interaction = FakeInteraction()

        async def render() -> None:
            command = self.bot.tree.get_command("tasks")
            assert command is not None
            await command.callback(list_interaction, page=1)

        asyncio.run(render())
        button = list_interaction.response.sent[0][1]["view"].children[0]
        interaction = FakeInteraction(user_id=999)
        asyncio.run(button.callback(interaction))

        self.assertFalse(self.store.get_task("private-task")["completed"])
        self.assertEqual(len(interaction.response.sent), 1)
        self.assertTrue(interaction.response.sent[0][1]["ephemeral"])

    def test_deleted_task_button_is_disabled_without_an_exception(self) -> None:
        self.store.add_task("後で削除される", task_id="deleted-task")
        list_interaction = FakeInteraction()

        async def render() -> None:
            command = self.bot.tree.get_command("tasks")
            assert command is not None
            await command.callback(list_interaction, page=1)

        asyncio.run(render())
        button = list_interaction.response.sent[0][1]["view"].children[0]
        self.store._connection.execute("DELETE FROM tasks WHERE id = ?", ("deleted-task",))
        self.store._connection.commit()
        interaction = FakeInteraction()
        asyncio.run(button.callback(interaction))

        self.assertTrue(button_item(button).disabled)
        self.assertEqual(button_item(button).label, "削除済み")
        self.assertTrue(interaction.response.edited)

    def test_live_buttons_use_one_dispatch_route_only(self):
        """Regression for real Discord error 40060 from duplicate callbacks."""
        self.store.add_task('一度だけ応答する', task_id='single-response')
        interaction = FakeInteraction()
        self.bot.tree.sync = AsyncMock(return_value=[])

        async def run():
            await self.bot.setup_hook()
            await self.bot.tree.get_command('tasks').callback(interaction, page=1)
            view = interaction.response.sent[0][1]['view']
            button = view.children[0]
            views = self.bot._connection._view_store
            views.add_view(view, message_id=777)
            click = FakeInteraction()
            click.message.id = 777
            dynamic_call = AsyncMock()
            ordinary_call = Mock(return_value=None)
            with patch.object(views, 'schedule_dynamic_item_call', dynamic_call), patch.object(view, '_dispatch_item', ordinary_call):
                views.dispatch_view(2, button.custom_id, click)
                await asyncio.sleep(0)
                dynamic_call.assert_awaited_once()
                ordinary_call.assert_not_called()
            self.assertIsInstance(button, discord.ui.DynamicItem)
            self.assertNotIn((2, button.custom_id), views._views.get(777, {}))
        asyncio.run(run())

    def test_dynamic_item_is_registered_for_restart_and_routes_to_store(self) -> None:
        self.store.add_task("再起動後も完了できる", task_id="restart-task")
        self.bot.tree.sync = AsyncMock(return_value=[])

        async def setup() -> None:
            await self.bot.setup_hook()

        asyncio.run(setup())
        dynamic_cls = self.bot._task_done_dynamic
        self.assertIsNotNone(dynamic_cls)
        registered = self.bot._connection._view_store._dynamic_items
        self.assertIn(dynamic_cls, registered.values())

        custom_id = "mailbot:task:done:restart-task"
        base_item = discord.ui.Button(custom_id=custom_id, label="完了 1: 再起動後も完了できる")
        interaction = FakeInteraction()
        interaction.message = SimpleNamespace(
            content="タスク一覧 (1/1)\n1. 再起動後も完了できる\n   状態: 未完了 / 期限: 期限なし"
        )
        match = re.fullmatch(
            r"mailbot:task:done:(?P<task_id>[A-Za-z0-9_.-]+)",
            custom_id,
        )

        async def route() -> None:
            assert match is not None
            dynamic_item = await dynamic_cls.from_custom_id(interaction, base_item, match)
            view = discord.ui.View(timeout=None)
            view.add_item(dynamic_item)
            dynamic_item._view = view
            await dynamic_item.callback(interaction)

        asyncio.run(route())
        self.assertTrue(self.store.get_task("restart-task")["completed"])
        self.assertTrue(base_item.disabled)
        self.assertIn("状態: 完了", interaction.response.edited[0][1]["content"])

    def test_dynamic_duplicate_titles_update_only_the_clicked_number(self) -> None:
        self.store.add_task("同じタイトル", task_id="same-one")
        self.store.add_task("同じタイトル", task_id="same-two")
        self.bot.tree.sync = AsyncMock(return_value=[])

        async def setup() -> None:
            await self.bot.setup_hook()

        asyncio.run(setup())
        dynamic_cls = self.bot._task_done_dynamic
        custom_id = "mailbot:task:done:same-two"
        base_item = discord.ui.Button(custom_id=custom_id, label="完了 2: 同じタイトル")
        interaction = FakeInteraction()
        interaction.message = SimpleNamespace(
            content=(
                "タスク一覧 (1/1)\n"
                "1. 同じタイトル\n   状態: 未完了 / 期限: 期限なし\n"
                "2. 同じタイトル\n   状態: 未完了 / 期限: 期限なし"
            )
        )
        match = re.fullmatch(
            r"mailbot:task:done:(?P<task_id>[A-Za-z0-9_.-]+)",
            custom_id,
        )

        async def route() -> None:
            assert match is not None
            dynamic_item = await dynamic_cls.from_custom_id(interaction, base_item, match)
            view = discord.ui.View(timeout=None)
            view.add_item(dynamic_item)
            dynamic_item._view = view
            await dynamic_item.callback(interaction)

        asyncio.run(route())
        content = interaction.response.edited[0][1]["content"]
        self.assertIn("1. 同じタイトル\n   状態: 未完了", content)
        self.assertIn("2. 同じタイトル\n   状態: 完了", content)

    def test_tasks_are_paginated_to_five_buttons(self) -> None:
        for index in range(6):
            self.store.add_task(f"タスク {index}", task_id=f"task-{index}")
        interaction = FakeInteraction()

        async def run() -> None:
            command = self.bot.tree.get_command("tasks")
            assert command is not None
            await command.callback(interaction, page=2)

        asyncio.run(run())
        message = str(interaction.response.sent[0][0][0])
        view = interaction.response.sent[0][1]["view"]
        self.assertIn("タスク一覧 (2/2)", message)
        self.assertIn("タスク 5", message)
        self.assertEqual(len(view.children), 1)
        self.assertLessEqual(len(view.children), 25)


if __name__ == "__main__":
    unittest.main()
