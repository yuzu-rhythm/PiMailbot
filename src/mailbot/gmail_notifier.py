"""Gmail polling, notification delivery and authorized reaction actions."""
import asyncio
import html
import math
import re
from urllib.parse import quote

from .gmail_client import GmailClient, GmailAuthError
from .gmail_sync import GmailSync
from .gemini_summary import GeminiSummarizer, SummaryError


def notification_text(message: dict, summary: dict | None = None, *, account_email: str | None = None) -> str:
    def clean(value, limit):
        text = ' '.join(html.unescape(str(value or '')).split())
        text = ''.join(c for c in text if c.isprintable())[:limit]
        text = re.sub(r'([\\`*_~|])', r'\\\1', text)
        return text.replace('<', '＜').replace('>', '＞')
    message_id = str(message.get('message_id', ''))
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,256}', message_id):
        raise ValueError('Invalid notification identity')
    ai_text = ''
    if summary:
        ai_text = ('🧠 プレビューのAI要約（要確認）\n'
                   f"{clean(summary.get('summary'), 200)}\n"
                   f"必要な対応: {clean(summary.get('next_action'), 100)}\n\n")
    mailbox = f"宛先アカウント: {clean(account_email, 180)}\n" if account_email else ''
    # authuser selects the correct Google mailbox even when browser account order differs.
    location = 'u/0/' if not account_email else '?authuser=' + quote(account_email, safe='')
    return ('📩 新着メール\n' + mailbox +
            f"差出人: {clean(message.get('sender'), 180)}\n"
            f"件名: {clean(message.get('subject'), 220) or '（件名なし）'}\n\n"
            + ai_text + f"プレビュー: {clean(message.get('snippet'), 500)}\n\n"
            f"Gmailで開く: https://mail.google.com/mail/{location}#all/{quote(message_id, safe='')}" )


class GmailNotifier:
    def __init__(self, settings, *, task_store=None, account_id=None, expected_email=None, channel_id=None):
        self.settings = settings
        self.task_store = task_store
        self.account_id = account_id
        self.expected_email = expected_email
        self.channel_id = channel_id
        self.engine = None
        self.client = None
        self._retry_after = 0
        self._operation_lock = asyncio.Lock()
        self._action_error = None
        self.summarizer = None
        self._summary_state = 'NOT_CONFIGURED'
        self._status = {'state': 'NOT_CONFIGURED'}

    def status(self):
        return {**self._status,
                'actions_enabled': self.settings.gmail_actions_enabled,
                'actions_authorized': bool(self.client and getattr(self.client, 'can_modify', False)),
                'tasks_enabled': self.settings.gmail_tasks_enabled,
                'task_storage_configured': self.task_store is not None,
                'last_action_error': self._action_error}

    def summary_status(self):
        return {'state': self._summary_state, 'source': 'subject_and_preview',
                'daily_limit': self.settings.gemini_daily_limit}

    def _initialize_summary(self):
        if not self.settings.gemini_enabled:
            return
        if not self.settings.gemini_preview_consent:
            self._summary_state = 'CONSENT_REQUIRED'
            return
        if self.settings.gemini_key_path is None:
            self._summary_state = 'KEY_REQUIRED'
            return
        try:
            self.summarizer = GeminiSummarizer(self.settings.gemini_key_path, self.settings.gemini_model)
            self._summary_state = 'CONFIGURED_NOT_CONNECTED'
        except SummaryError as error:
            self._summary_state = str(error)

    async def _summarize(self, message):
        if self.summarizer is None:
            return None
        try:
            cached = await asyncio.to_thread(self.engine.reserve_summary, message['message_id'], self.settings.gemini_daily_limit)
            if cached['state'] == 'DONE':
                self._summary_state = 'CACHED'
                return {'summary': cached['summary'], 'next_action': cached['next_action']}
            if not cached['reserved']:
                self._summary_state = str(cached['state'])
                return None
            try:
                result = await asyncio.to_thread(self.summarizer.summarize, message)
            except SummaryError as error:
                await asyncio.to_thread(self.engine.finish_summary, message['message_id'], None)
                self._summary_state = str(error)
                return None
            await asyncio.to_thread(self.engine.finish_summary, message['message_id'], result)
            self._summary_state = 'ACTIVE'
            return result
        except Exception:
            # Summary storage/network failures must not block mail delivery.
            self._summary_state = 'SUMMARY_UNAVAILABLE'
            return None

    def initialize(self):
        if not self.settings.gmail_enabled:
            return False
        if self.settings.gmail_token_path is None:
            self._status = {'state': 'AUTH_REQUIRED'}
            return False
        try:
            self.client = GmailClient(self.settings.gmail_token_path)
            if self.expected_email:
                profile = self.client.get_profile()
                if str(profile.get('emailAddress', '')).casefold() != self.expected_email.casefold():
                    self.client = None
                    self._status = {'state': 'ACCOUNT_MISMATCH'}
                    return False
            path = self.settings.gmail_database_path or self.settings.database_path.with_name('gmail-notifications.sqlite3')
            self.engine = GmailSync(path)
            if self.expected_email:
                self.engine.bind_account(self.expected_email)
            self._status = self.engine.status()
            self._initialize_summary()
            return True
        except GmailAuthError:
            self._status = {'state': 'AUTH_REQUIRED'}
        except Exception:
            self._status = {'state': 'ERROR'}
        if self.engine is not None:
            self.engine.close()
            self.engine = None
        self.client = None
        return False

    async def cycle(self, bot, discord):
        async with self._operation_lock:
            return await self._cycle(bot, discord)

    async def _cycle(self, bot, discord):
        channel_id = self.channel_id or self.settings.reminder_channel_id
        if not self.settings.discord_allowlist_configured or channel_id is None:
            self._status = {'state': 'CHANNEL_NOT_ALLOWED'}
            return False
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        if (getattr(channel, 'id', None) not in self.settings.allowed_channel_ids or
                getattr(getattr(channel, 'guild', None), 'id', None) not in self.settings.allowed_guild_ids):
            self._status = {'state': 'CHANNEL_NOT_ALLOWED'}
            return False
        result = await asyncio.to_thread(self.engine.poll_once, self.client)
        retry = result.get('retry_after')
        self._retry_after = min(3600, retry) if isinstance(retry, (int, float)) and math.isfinite(retry) and retry > 0 else 0
        self._status = await asyncio.to_thread(self.engine.status)
        await self._archive_registered_tasks(bot)
        from .mail_history import cleanup
        await cleanup(self, bot)
        pending = await asyncio.to_thread(self.engine.pending, 5)
        for message in pending:
            summary = await self._summarize(message)
            text = notification_text(message, summary, account_email=self.expected_email)
            from .mail_triage import make_view
            text += '\n\n要対応 ／ あとで読む ／ 保管 ／ 不要（ゴミ箱）を選んでください。'
            sent = await channel.send(text, view=make_view(discord, bot.mail_accounts),
                                      allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            await asyncio.to_thread(self.engine.mark_sent, message['message_id'], str(sent.id),
                                    guild_id=channel.guild.id, channel_id=channel.id)
        self._status = await asyncio.to_thread(self.engine.status)
        return result.get('state') in {'READY', 'BASELINED', 'PARTIAL'}

    async def _archive_registered_tasks(self, bot):
        if (not self.settings.gmail_task_archive_enabled or self.task_store is None or
                not self.settings.gmail_actions_enabled or not self.client.can_modify):
            return
        for row in await asyncio.to_thread(self.engine.task_archives):
            # Recheck access against the original exact notification location.
            if not self.settings.discord_authorized(guild_id=int(row['guild_id']),
                    channel_id=int(row['channel_id']), user_id=int(row['user_id'])):
                continue
            if not await asyncio.to_thread(self.task_store.todoist_task_registered, row['task_id']):
                continue
            try:
                # Trash takes precedence; never remove it or bring a message back.
                if await asyncio.to_thread(self.engine.action_done, row['discord_id'], 'trash'):
                    await asyncio.to_thread(self.engine.finish_task_archive, row['discord_id'])
                    continue
                if not await asyncio.to_thread(self.engine.action_done, row['discord_id'], 'archive'):
                    await asyncio.to_thread(self.client.apply_action, row['message_id'], 'archive')
                    await asyncio.to_thread(self.engine.complete_action, row['discord_id'], 'archive', row['user_id'])
                from .mail_triage import finish_task
                await asyncio.to_thread(finish_task, self.engine, self.client, row['discord_id'], row['message_id'])
                await asyncio.to_thread(self.engine.queue_cleanup, row['discord_id'])
                await asyncio.to_thread(self.engine.finish_task_archive, row['discord_id'])
                # A failed/deleted Discord acknowledgement must not reactivate
                # a completed Gmail archive after cleanup removes the notice.
                try:
                    channel = bot.get_channel(int(row['channel_id']))
                    if channel is None:
                        channel = await bot.fetch_channel(int(row['channel_id']))
                    await channel.get_partial_message(int(row['discord_id'])).add_reaction('🗄️')
                except Exception:
                    pass
                self._action_error = None
            except Exception:
                self._action_error = 'TASK_ARCHIVE_RETRY_PENDING'
                await asyncio.to_thread(self.engine.finish_task_archive, row['discord_id'], error='ARCHIVE_FAILED')

    async def handle_reaction(self, bot, payload):
        action = {'✅': 'read', '📥': 'archive', '📝': 'task', '🗑️': 'trash', '🗑': 'trash'}.get(str(payload.emoji))
        if (not action or
                (action == 'task' and not self.settings.gmail_tasks_enabled) or
                (action == 'trash' and not self.settings.gmail_trash_enabled) or
                (action != 'task' and not self.settings.gmail_actions_enabled) or
                not self.settings.gmail_enabled or self.engine is None or
                getattr(getattr(bot, 'user', None), 'id', None) == payload.user_id or
                not self.settings.discord_authorized(guild_id=payload.guild_id,
                    channel_id=payload.channel_id, user_id=payload.user_id)):
            return
        async with self._operation_lock:
            try:
                target = await asyncio.to_thread(self.engine.reaction_target,
                    payload.message_id, payload.guild_id, payload.channel_id)
                if target is None:
                    return
                if action == 'task' and self.task_store is None:
                    self._action_error = 'TASK_STORAGE_UNAVAILABLE'
                    return
                if action != 'task' and (self.client is None or not self.client.can_modify):
                    self._action_error = 'MODIFY_AUTH_REQUIRED'
                    return
                done = await asyncio.to_thread(self.engine.action_done, payload.message_id, action)
                if not done:
                    if action == 'task':
                        details = await asyncio.to_thread(self.engine.reaction_details,
                            payload.message_id, payload.guild_id, payload.channel_id)
                        if details is None or details['message_id'] != target:
                            return
                        task = await asyncio.to_thread(self.task_store.add_mail_task, details['subject'],
                            guild_id=payload.guild_id, channel_id=payload.channel_id, message_id=target)
                        if self.settings.gmail_task_archive_enabled:
                            await asyncio.to_thread(self.engine.queue_task_archive, payload.message_id,
                                target, task['id'], payload.user_id)
                    else:
                        await asyncio.to_thread(self.client.apply_action, target, action)
                    # Replay is safe: task identity is deterministic and label removal is idempotent.
                    await asyncio.to_thread(self.engine.complete_action, payload.message_id, action, payload.user_id)
                self._action_error = None
            except GmailAuthError:
                self._action_error = 'MODIFY_AUTH_REQUIRED'
                return
            except Exception:
                # No exception body: provider responses may include private data.
                self._action_error = 'ACTION_FAILED_READD_REACTION_TO_RETRY'
                return
            try:
                channel = bot.get_channel(payload.channel_id)
                if channel is None:
                    channel = await bot.fetch_channel(payload.channel_id)
                await channel.get_partial_message(payload.message_id).add_reaction(
                    {'read': '👀', 'archive': '🗄️', 'task': '📋', 'trash': '🚮'}[action])
            except Exception:
                self._action_error = 'ACTION_APPLIED_ACK_FAILED'

    async def run(self, bot, discord):
        failures = 0
        try:
            while not bot.is_closed():
                try:
                    if self.engine is None and not await asyncio.to_thread(self.initialize):
                        failures = min(failures + 1, 5)
                        await asyncio.sleep(min(3600, self.settings.gmail_poll_seconds * (2 ** failures)))
                        continue
                    success = await self.cycle(bot, discord)
                    failures = 0 if success else min(failures + 1, 5)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures = min(failures + 1, 5)
                    self._status = {**self._status, 'state': 'BACKOFF'}
                await asyncio.sleep(max(self._retry_after, min(3600, self.settings.gmail_poll_seconds * (2 ** failures))))
        finally:
            if self.engine is not None:
                await asyncio.to_thread(self.engine.close)
