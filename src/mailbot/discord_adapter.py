"""Optional discord.py adapter for the local mailbot store.

Importing this module does not import discord.py.  The package remains usable
offline, while an explicit ``discord`` command can fail with a clear setup
message when the optional dependency is absent.
"""

import asyncio
from datetime import datetime, timezone
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from .attachments import AttachmentError, AttachmentStore
from .config import Settings
from .storage import SqliteStore


class DiscordDependencyUnavailable(RuntimeError):
    """Raised when the optional Discord dependency is not installed."""


class DiscordConfigurationError(RuntimeError):
    """Raised when a Discord run lacks a token or explicit allowlists."""


_TASK_BUTTON_PREFIX = "mailbot:task:done:"
_TASK_BUTTON_MAX_LENGTH = 100
_TASK_PAGE_SIZE = 5
_TASK_TITLE_DISPLAY_LIMIT = 240
_TASK_BUTTON_LABEL_LIMIT = 70
_TASK_TIMEZONE = ZoneInfo("Asia/Tokyo")


def _display_task_title(title: object, *, limit: int = _TASK_TITLE_DISPLAY_LIMIT) -> str:
    """Return a compact single-line task title for Discord output."""

    text = " ".join(str(title or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(1, limit - 1)]}…"


def _format_task_due(due_at: object) -> str:
    """Format a stored Unix timestamp in the user's configured display zone."""

    if due_at is None:
        return "期限なし"
    if isinstance(due_at, bool) or not isinstance(due_at, (int, float)):
        return "期限不明"
    try:
        due = datetime.fromtimestamp(due_at, timezone.utc).astimezone(_TASK_TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return "期限不明"
    return f"{due:%Y/%m/%d %H:%M} (日本時間)"


def _task_button_custom_id(task_id: object) -> str | None:
    """Build a Discord-safe private task button ID, or omit an oversized ID."""

    if not isinstance(task_id, str) or not task_id:
        return None
    custom_id = f"{_TASK_BUTTON_PREFIX}{task_id}"
    if len(custom_id) > _TASK_BUTTON_MAX_LENGTH:
        # SqliteStore permits legacy IDs up to 128 characters while Discord
        # limits component custom_id values to 100 characters.  Such tasks
        # remain available through /task_done and autocomplete.
        return None
    return custom_id


def _task_page_content(
    tasks: list[dict[str, object]],
    *,
    page: int,
    page_count: int,
) -> str:
    """Render a bounded, UUID-free task page."""

    lines = [f"タスク一覧 ({page}/{page_count})"]
    for number, task in enumerate(tasks, start=1):
        status = "完了" if task.get("completed") else "未完了"
        title = _display_task_title(task.get("title"))
        due = _format_task_due(task.get("due_at"))
        lines.append(f"{number}. {title}\n   状態: {status} / 期限: {due}")
    return "\n".join(lines)


def _task_button_label(title: object, *, number: int, completed: bool = False) -> str:
    prefix = f"完了済み {number}: " if completed else f"完了 {number}: "
    remaining = max(1, _TASK_BUTTON_LABEL_LIMIT - len(prefix))
    return f"{prefix}{_display_task_title(title, limit=remaining)}"


def _mark_task_message_completed(
    content: object,
    task: dict[str, object],
    *,
    button: object = None,
) -> str | None:
    """Update a task-list status line when DynamicItem rebuilt a generic View."""

    if not isinstance(content, str) or not content:
        return None
    title = _display_task_title(task.get("title"))
    lines = content.splitlines()
    button_item = getattr(button, "item", button)
    label = str(getattr(button_item, "label", ""))
    number_match = re.match(r"(?:完了|完了済み)\s+(\d+):", label)
    if number_match is not None:
        number_prefix = f"{number_match.group(1)}. "
        for index, line in enumerate(lines[:-1]):
            if line.startswith(number_prefix) and "状態: 未完了" in lines[index + 1]:
                lines[index + 1] = lines[index + 1].replace(
                    "状態: 未完了",
                    "状態: 完了",
                    1,
                )
                return "\n".join(lines)
        # A repeated click on an already-completed numbered row must never
        # fall back to another row that happens to have the same title.
        if content.startswith("タスクを保存しました:") and "状態: 完了" not in content:
            return f"{content}\n状態: 完了"
        return content
    for index, line in enumerate(lines[:-1]):
        if title and title in line and "状態: 未完了" in lines[index + 1]:
            lines[index + 1] = lines[index + 1].replace("状態: 未完了", "状態: 完了", 1)
            return "\n".join(lines)
    return None


def _public_status(
    settings: Settings,
    store: SqliteStore,
    *,
    connected: bool = False,
) -> dict[str, object]:
    return {
        "config": settings.public_dict(),
        "storage": store.status(),
        "integrations": {
            "discord": (
                "CONNECTED"
                if connected
                else (
                    "CONFIGURED_NOT_CONNECTED"
                    if settings.discord_token_configured and settings.discord_allowlist_configured
                    else "NOT_CONFIGURED"
                )
            ),
            "gmail": "NOT_CONFIGURED",
            "google_tasks": "NOT_CONFIGURED",
            "llm": "NOT_CONFIGURED",
        },
    }


class DiscordAdapter:
    """Register a small slash-command surface around ``SqliteStore``."""

    def __init__(self, store: SqliteStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        from .gmail_accounts import GmailAccounts
        self.gmail = GmailAccounts(settings, task_store=store)
        from .todoist_sync import TodoistSync
        self.todoist = TodoistSync(settings, store)
        self.attachments = AttachmentStore(
            settings.image_dir,
            store,
            max_bytes=settings.max_image_bytes,
        )

    def build_bot(self) -> Any:
        try:
            import discord
            from discord.ext import commands
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise DiscordDependencyUnavailable(
                "discord.py is not installed; use `pip install -e '.[discord]'`"
            ) from exc

        intents = discord.Intents.none()
        intents.guild_reactions = self.settings.gmail_actions_enabled or self.settings.gmail_tasks_enabled
        adapter = self

        async def authorized(interaction: Any) -> bool:
            guild_id = getattr(interaction, "guild_id", None)
            channel_id = getattr(interaction, "channel_id", None)
            user = getattr(interaction, "user", None)
            user_id = getattr(user, "id", None)
            allowed = adapter.settings.discord_authorized(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            if allowed:
                return True
            await interaction.response.send_message(
                "このBotは許可リスト未設定または対象外のため利用できません。",
                ephemeral=True,
            )
            return False

        async def respond(
            interaction: Any,
            message: str,
            *,
            ephemeral: bool = False,
            view: Any = None,
        ) -> None:
            """Send bounded chunks so Discord's 2,000 character limit is met."""

            chunks = [message[index : index + 1900] for index in range(0, len(message), 1900)]
            if not chunks:
                chunks = [""]
            send_kwargs: dict[str, object] = {"ephemeral": ephemeral}
            if view is not None:
                send_kwargs["view"] = view
            await interaction.response.send_message(chunks[0], **send_kwargs)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=ephemeral)

        async def edit_component(
            interaction: Any,
            *,
            view: Any,
            content: str | None = None,
        ) -> None:
            """Acknowledge a component interaction while updating its view."""

            edit_message = getattr(interaction.response, "edit_message", None)
            if callable(edit_message):
                edit_kwargs: dict[str, object] = {"view": view}
                if content is not None:
                    edit_kwargs["content"] = content
                await edit_message(**edit_kwargs)
                return
            # This fallback is useful for small test doubles and keeps a
            # stale component from remaining unacknowledged if a client
            # implementation exposes only send_message.
            await interaction.response.send_message(
                content or "タスクの状態を更新しました。",
                ephemeral=True,
            )

        def set_button_state(button: Any, *, state: str, title: object = "") -> None:
            """Update either a normal Button or a DynamicItem's wrapped item."""

            target = getattr(button, "item", button)
            target.disabled = True
            target.style = discord.ButtonStyle.secondary
            if state == "deleted":
                target.label = "削除済み"
            else:
                current_label = str(getattr(target, "label", ""))
                if current_label.startswith("完了済み"):
                    label = current_label
                elif current_label.startswith("完了"):
                    label = f"完了済み{current_label[2:]}"
                else:
                    label = _task_button_label(title, number=1, completed=True)
                target.label = label[:_TASK_BUTTON_LABEL_LIMIT]

        async def complete_task_from_interaction(
            interaction: Any,
            task_id: str,
            button: Any,
        ) -> None:
            """Authorize and idempotently complete the task behind a button."""

            # The component path has the same guild/channel/user default-deny
            # guard as slash commands.  Check it before touching SQLite.
            if not await authorized(interaction):
                return

            try:
                task = adapter.store.get_task(task_id)
            except (KeyError, TypeError, ValueError):
                task = None
            view = getattr(button, "view", None)
            if task is None:
                set_button_state(button, state="deleted")
                await edit_component(interaction, view=view)
                return

            try:
                if not bool(task.get("completed")):
                    task = adapter.store.complete_task(task_id)
            except (KeyError, TypeError, ValueError):
                # A task can disappear between get_task and complete_task.
                # Treat that as a safe stale-message result.
                set_button_state(button, state="deleted")
                await edit_component(interaction, view=view)
                return

            set_button_state(button, state="completed", title=task.get("title", ""))
            update_task = getattr(view, "update_task", None)
            if callable(update_task):
                update_task(task)
            render_content = getattr(view, "render_content", None)
            content = render_content() if callable(render_content) else None
            if content is None:
                message = getattr(interaction, "message", None)
                content = _mark_task_message_completed(
                    getattr(message, "content", None),
                    task,
                    button=button,
                )
            await edit_component(interaction, view=view, content=content)

        class TaskCompletionButton(discord.ui.Button):
            """A task completion button retained while this process is online."""

            def __init__(
                self,
                *,
                task_id: str,
                title: object,
                number: int,
                completed: bool,
                row: int,
            ) -> None:
                custom_id = _task_button_custom_id(task_id)
                if custom_id is None:
                    raise ValueError("task ID cannot fit in a Discord button custom_id")
                super().__init__(
                    style=(
                        discord.ButtonStyle.secondary
                        if completed
                        else discord.ButtonStyle.primary
                    ),
                    label=_task_button_label(title, number=number, completed=completed),
                    disabled=completed,
                    custom_id=custom_id,
                    row=row,
                )
                self.task_id = task_id

            async def callback(self, interaction: Any) -> None:
                await complete_task_from_interaction(interaction, self.task_id, self)

        class TaskListView(discord.ui.View):
            """Persistent task controls for one bounded page."""

            def __init__(
                self,
                tasks: list[dict[str, object]],
                *,
                page: int,
                page_count: int,
            ) -> None:
                super().__init__(timeout=None)
                self._tasks = [dict(task) for task in tasks]
                self.page = page
                self.page_count = page_count
                for number, task in enumerate(self._tasks, start=1):
                    task_id = task.get("id")
                    if not isinstance(task_id, str):
                        continue
                    if _task_button_custom_id(task_id) is None:
                        # The slash command and title autocomplete remain the
                        # safe completion route for oversized legacy IDs.
                        continue
                    button = TaskCompletionButton(
                        task_id=task_id,
                        title=task.get("title", ""),
                        number=number,
                        completed=bool(task.get("completed")),
                        row=(number - 1) // 5,
                    )
                    # Register a custom_id through one route only. discord.py
                    # dispatches dynamic AND ordinary items when both match.
                    if task_done_dynamic is not None:
                        self.add_item(task_done_dynamic(task_id, item=button))
                    else:
                        self.add_item(button)

            def update_task(self, task: dict[str, object]) -> None:
                task_id = task.get("id")
                for index, current in enumerate(self._tasks):
                    if current.get("id") == task_id:
                        self._tasks[index] = dict(task)
                        break

            def render_content(self) -> str:
                return _task_page_content(
                    self._tasks,
                    page=self.page,
                    page_count=self.page_count,
                )

        dynamic_item_base = getattr(discord.ui, "DynamicItem", None)
        task_done_dynamic: Any = None
        if dynamic_item_base is not None:
            dynamic_template = (
                rf"{re.escape(_TASK_BUTTON_PREFIX)}"
                rf"(?P<task_id>[A-Za-z0-9_.-]+)"
            )

            class TaskDoneDynamic(dynamic_item_base[discord.ui.Button], template=dynamic_template):
                """Restart-safe route for task buttons from old messages."""

                def __init__(self, task_id: str, *, item: Any | None = None) -> None:
                    self.task_id = task_id
                    if item is None:
                        custom_id = _task_button_custom_id(task_id)
                        if custom_id is None:
                            raise ValueError("task ID cannot fit in a Discord button custom_id")
                        item = discord.ui.Button(
                            style=discord.ButtonStyle.primary,
                            label="完了",
                            custom_id=custom_id,
                        )
                    super().__init__(item)

                @classmethod
                async def from_custom_id(
                    cls,
                    interaction: Any,
                    item: Any,
                    match: re.Match[str],
                    /,
                ) -> "TaskDoneDynamic":
                    del interaction
                    return cls(match.group("task_id"), item=item)

                async def callback(self, interaction: Any) -> None:
                    await complete_task_from_interaction(interaction, self.task_id, self)

            task_done_dynamic = TaskDoneDynamic

        class MailbotClient(commands.Bot):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.mail_accounts = adapter.gmail
                self._reminder_task: asyncio.Task[None] | None = None
                self._gmail_task: asyncio.Task[None] | None = None
                self._todoist_task: asyncio.Task[None] | None = None

            async def setup_hook(self) -> None:
                from .mail_triage import make_view
                self.add_view(make_view(discord, adapter.gmail))
                # DynamicItem registration is local and makes old task
                # messages work after a restart without loading every task as
                # a separate persistent View.  The fallback is for older
                # discord.py versions without DynamicItem support.
                if task_done_dynamic is not None:
                    self.add_dynamic_items(task_done_dynamic)
                else:  # pragma: no cover - current supported runtime is 2.7+
                    for task in adapter.store.list_tasks(include_completed=False):
                        task_id = task.get("id")
                        if isinstance(task_id, str) and _task_button_custom_id(task_id):
                            self.add_view(TaskListView([task], page=1, page_count=1))
                # Syncing commands is an intentional external action and only
                # occurs after the user explicitly runs the Discord command.
                await self.tree.sync()

            async def close(self) -> None:
                if self._todoist_task is not None:
                    self._todoist_task.cancel()
                    await asyncio.gather(self._todoist_task, return_exceptions=True)
                    self._todoist_task = None
                if self._gmail_task is not None:
                    self._gmail_task.cancel()
                    await asyncio.gather(self._gmail_task, return_exceptions=True)
                    self._gmail_task = None
                if self._reminder_task is not None:
                    self._reminder_task.cancel()
                    await asyncio.gather(self._reminder_task, return_exceptions=True)
                    self._reminder_task = None
                await super().close()

        bot = MailbotClient(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # Expose the class for offline tests and diagnostics without exposing
        # task IDs or requiring a network connection.
        bot._task_done_dynamic = task_done_dynamic

        @bot.event
        async def on_raw_reaction_add(payload: Any) -> None:
            await adapter.gmail.handle_reaction(bot, payload)

        @bot.event
        async def on_ready() -> None:
            if adapter.settings.todoist_enabled and (bot._todoist_task is None or bot._todoist_task.done()):
                bot._todoist_task = asyncio.create_task(adapter.todoist.run())
            if adapter.settings.gmail_enabled and (bot._gmail_task is None or bot._gmail_task.done()):
                bot._gmail_task = asyncio.create_task(adapter.gmail.run(bot, discord))
            if bot._reminder_task is None or bot._reminder_task.done():
                bot._reminder_task = asyncio.create_task(
                    adapter._reminder_loop(bot, discord)
                )

        @bot.tree.command(name="ping", description="オフラインBotの応答確認")
        async def ping(interaction: Any) -> None:
            if await authorized(interaction):
                await respond(interaction, "pong")

        @bot.tree.command(name="status", description="ローカル状態と未設定連携を表示")
        async def status(interaction: Any) -> None:
            if await authorized(interaction):
                state = _public_status(adapter.settings, adapter.store, connected=True)
                state['integrations']['gmail'] = adapter.gmail.status()['state']
                state['gmail'] = adapter.gmail.status()
                state['integrations']['todoist'] = adapter.todoist.status()['state']
                state['todoist'] = adapter.todoist.status()
                state['integrations']['llm'] = adapter.gmail.summary_status()['state']
                state['llm'] = adapter.gmail.summary_status()
                payload = json.dumps(
                    state,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                await respond(interaction, f"```json\n{payload}\n```")

        @bot.tree.command(name="mail_history", description="このチャンネルのメール処理履歴を短く表示・取り消す")
        async def mail_history(interaction: Any, page: int = 1) -> None:
            from .mail_history import show_history
            await show_history(adapter.gmail, interaction, discord, page)

        async def content_categories(interaction: discord.Interaction, message: discord.Message):
            from .mail_categories import handle
            await handle(adapter.gmail, interaction, target_id=message.id)

        bot.tree.add_command(discord.app_commands.ContextMenu(
            name='メールの分類', callback=content_categories))

        @bot.tree.command(name="mail_review", description="指定日以降の過去メールを確認・最大10件ずつ見直す")
        async def mail_review(interaction: Any, since: str, load: bool = False) -> None:
            if not await authorized(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            from .mail_review import review
            await review(adapter.gmail, interaction, discord, since, load)

        @bot.tree.command(name="task_add", description="ローカルタスクを追加")
        async def task_add(interaction: Any, title: str, due: str | None = None) -> None:
            if not await authorized(interaction):
                return
            try:
                task = adapter.store.add_task(title, due_at=due)
            except (TypeError, ValueError) as exc:
                await respond(interaction, f"入力エラー: {exc}", ephemeral=True)
                return
            await respond(
                interaction,
                f"タスクを保存しました: {_display_task_title(task['title'])}\n"
                f"期限: {_format_task_due(task.get('due_at'))}",
                view=TaskListView([task], page=1, page_count=1),
            )

        @bot.tree.command(name="tasks", description="ローカルタスク一覧")
        async def tasks(interaction: Any, page: int = 1) -> None:
            if not await authorized(interaction):
                return
            if page < 1:
                await respond(interaction, "ページ番号は1以上で指定してください。", ephemeral=True)
                return
            rows = adapter.store.list_tasks(include_completed=True)
            if not rows:
                await respond(interaction, "タスクはありません。")
                return
            page_count = (len(rows) + _TASK_PAGE_SIZE - 1) // _TASK_PAGE_SIZE
            if page > page_count:
                await respond(
                    interaction,
                    f"そのページはありません。1〜{page_count}を指定してください。",
                    ephemeral=True,
                )
                return
            start = (page - 1) * _TASK_PAGE_SIZE
            page_rows = rows[start : start + _TASK_PAGE_SIZE]
            view = TaskListView(page_rows, page=page, page_count=page_count)
            await respond(interaction, view.render_content(), view=view)

        @bot.tree.command(name="task_done", description="ローカルタスクを完了")
        async def task_done(interaction: Any, task_id: str) -> None:
            if not await authorized(interaction):
                return
            try:
                task = adapter.store.get_task(task_id)
            except (KeyError, TypeError, ValueError) as exc:
                del exc
                task = None
            if task is None:
                await respond(interaction, "そのタスクは見つかりません。", ephemeral=True)
                return
            was_completed = bool(task.get("completed"))
            try:
                if not was_completed:
                    task = adapter.store.complete_task(task_id)
            except (KeyError, TypeError, ValueError):
                await respond(interaction, "そのタスクは見つかりません。", ephemeral=True)
                return
            if was_completed:
                await respond(interaction, f"すでに完了しています: {_display_task_title(task['title'])}")
            else:
                await respond(interaction, f"完了にしました: {_display_task_title(task['title'])}")

        @task_done.autocomplete("task_id")
        async def task_done_autocomplete(interaction: Any, current: str) -> list[Any]:
            if not adapter.settings.discord_authorized(
                guild_id=getattr(interaction, "guild_id", None),
                channel_id=getattr(interaction, "channel_id", None),
                user_id=getattr(getattr(interaction, "user", None), "id", None),
            ):
                return []
            needle = (current or "").strip().casefold()
            choices = []
            for task in adapter.store.list_tasks(include_completed=True):
                title = str(task.get("title", ""))
                if needle and needle not in title.casefold():
                    continue
                task_id = task.get("id")
                if not isinstance(task_id, str):
                    continue
                choices.append(
                    discord.app_commands.Choice(
                        name=_display_task_title(title, limit=100),
                        value=task_id,
                    )
                )
                if len(choices) >= 25:
                    break
            return choices

        @bot.tree.command(name="reminders", description="期限超過の未送信リマインダー一覧")
        async def reminders(interaction: Any) -> None:
            if not await authorized(interaction):
                return
            rows = adapter.store.pending_reminders()
            if not rows:
                await respond(interaction, "未送信の期限超過リマインダーはありません。")
                return
            lines = [
                f"{_display_task_title(task['title'])} (期限: {_format_task_due(task['due_at'])})"
                for task in rows[:25]
            ]
            await respond(interaction, "\n".join(lines))

        @bot.tree.command(name="game_credit", description="ゲームの手動クレジットを記録")
        async def game_credit(
            interaction: Any,
            game: str,
            credits: int,
            note: str | None = None,
        ) -> None:
            if not await authorized(interaction):
                return
            try:
                row = adapter.store.add_game_credit(game, credits, note=note or "")
            except (TypeError, ValueError) as exc:
                await respond(interaction, f"入力エラー: {exc}", ephemeral=True)
                return
            await respond(
                interaction,
                f"クレジットを記録しました: {row['game']} / {row['credits']}"
            )

        @bot.tree.command(name="game_credits", description="手動ゲームクレジット一覧")
        async def game_credits(interaction: Any, game: str | None = None) -> None:
            if not await authorized(interaction):
                return
            rows = adapter.store.list_game_credits(game=game)
            if not rows:
                await respond(interaction, "クレジット記録はありません。")
                return
            lines = [
                f"{row['game']}: {row['credits']} ({row['note'] or 'メモなし'})"
                for row in rows[-25:]
            ]
            await respond(interaction, "\n".join(lines))

        @bot.tree.command(name="image_save", description="画像添付をローカル保存")
        async def image_save(interaction: Any, attachment: discord.Attachment) -> None:
            if not await authorized(interaction):
                return
            if attachment.size and attachment.size > adapter.settings.max_image_bytes:
                await respond(
                    interaction,
                    f"画像がサイズ上限 ({adapter.settings.max_image_bytes} bytes) を超えています。",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                data = await attachment.read()
                row = adapter.attachments.save_image(
                    attachment.filename,
                    data,
                    content_type=attachment.content_type,
                )
            except (AttachmentError, TypeError, ValueError, discord.HTTPException) as exc:
                await interaction.followup.send(f"画像を保存できません: {exc}", ephemeral=True)
                return
            await interaction.followup.send(
                f"画像を保存しました: `{row['stored_name']}` ({row['size']} bytes)"
            )

        return bot

    async def _reminder_loop(self, bot: Any, discord: Any) -> None:
        """Deliver due local reminders at least once while Discord is online.

        SQLite is updated only after ``channel.send`` succeeds.  A process
        crash between those two operations can produce a duplicate on restart,
        so this is deliberately at-least-once delivery rather than an
        exactly-once claim.
        """

        while not bot.is_closed():
            try:
                await self._deliver_pending_reminders(bot, discord)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep the worker alive through transient Discord or local
                # errors without putting response bodies or tokens in logs.
                pass
            await asyncio.sleep(60)

    async def _deliver_pending_reminders(self, bot: Any, discord: Any) -> None:
        channel_id = self.settings.reminder_channel_id
        if channel_id is None:
            return
        pending = self.store.pending_reminders()
        if not pending:
            return
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        guild = getattr(channel, "guild", None)
        guild_id = getattr(guild, "id", None)
        if guild_id not in self.settings.allowed_guild_ids:
            return
        if getattr(channel, "id", channel_id) not in self.settings.allowed_channel_ids:
            return
        for task in pending:
            try:
                await channel.send(
                    f"リマインダー: {task['title']} (due={task['due_at']})",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                return
            self.store.mark_reminder_sent(str(task["id"]))

    def run(self) -> None:
        if not self.settings.discord_token_configured:
            raise DiscordConfigurationError(
                "DISCORD_BOT_TOKEN is not configured; Discord remains disabled"
            )
        if not self.settings.discord_allowlist_configured:
            raise DiscordConfigurationError(
                "DISCORD_ALLOWED_GUILDS, DISCORD_ALLOWED_CHANNELS, and "
                "DISCORD_ALLOWED_USERS are required; default deny remains active"
            )
        bot = self.build_bot()
        # discord.py owns the network loop from this point.  The token is
        # never included in a message or log emitted by this adapter.
        bot.run(self.settings.discord_token)


def run_discord(settings: Settings, store: SqliteStore | None = None) -> None:
    """Run the optional Discord adapter after all local safety gates pass."""

    owned_store = store is None
    active_store = store or SqliteStore(settings.database_path)
    try:
        DiscordAdapter(active_store, settings).run()
    finally:
        if owned_store:
            active_store.close()
