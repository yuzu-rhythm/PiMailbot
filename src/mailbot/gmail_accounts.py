"""Explicit private mailbox routing with isolated authentication and outboxes."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import re
import stat

from .gmail_notifier import GmailNotifier


class AccountConfigurationError(ValueError):
    pass


def load_accounts(path, settings):
    """Reject ambiguous routes; a manifest never expands Discord allowlists."""
    try:
        path = Path(path)
        info, parent = path.lstat(), path.parent.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) & 0o077):
            raise ValueError()
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError()
        rows = data['accounts']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
            raise ValueError()
        used_ids, used_emails, used_channels, used_paths = set(), set(), set(), set()
        protected = {settings.database_path.resolve()}
        for secret in (settings.todoist_token_path, settings.gemini_key_path):
            if secret is not None:
                protected.add(secret.resolve())
        validated = []
        for row in rows:
            account_id, email = row['id'], row['email']
            if not isinstance(account_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', account_id):
                raise ValueError()
            if not isinstance(email, str) or not re.fullmatch(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}', email):
                raise ValueError()
            email = email.casefold()
            enabled = row.get('enabled', False)
            if type(enabled) is not bool:
                raise ValueError()
            guild_id, channel_id = row['guild_id'], row['channel_id']
            if (type(guild_id) is not int or type(channel_id) is not int or
                    guild_id <= 0 or channel_id <= 0):
                raise ValueError()
            if enabled and (guild_id not in settings.allowed_guild_ids or channel_id not in settings.allowed_channel_ids):
                raise ValueError()
            paths = []
            for key in ('token_path', 'database_path'):
                candidate = Path(row[key])
                if not candidate.is_absolute() or any(p.is_symlink() for p in (candidate, *candidate.parents)):
                    raise ValueError()
                candidate = candidate.resolve()
                if candidate in protected or candidate in used_paths:
                    raise ValueError()
                paths.append(candidate)
                used_paths.add(candidate)
            if paths[0].suffix != '.json' or paths[1].suffix != '.sqlite3':
                raise ValueError()
            if account_id in used_ids or email in used_emails or channel_id in used_channels:
                raise ValueError()
            used_ids.add(account_id)
            used_emails.add(email)
            used_channels.add(channel_id)
            validated.append({'id': account_id, 'email': email, 'enabled': enabled,
                              'guild_id': guild_id, 'channel_id': channel_id,
                              'token_path': paths[0], 'database_path': paths[1]})
        return validated
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise AccountConfigurationError('GMAIL_ACCOUNTS_CONFIG_INVALID') from None


class GmailAccounts:
    def __init__(self, settings, *, task_store=None):
        self.settings = settings
        self.workers = []
        self._config_error = False
        if settings.gmail_accounts_path is None:
            self.workers = [GmailNotifier(settings, task_store=task_store)]
            return
        try:
            for row in load_accounts(settings.gmail_accounts_path, settings):
                worker_settings = replace(settings, gmail_enabled=row['enabled'],
                    gmail_token_path=row['token_path'], gmail_database_path=row['database_path'])
                self.workers.append(GmailNotifier(worker_settings, task_store=task_store,
                    account_id=row['id'], expected_email=row['email'], channel_id=row['channel_id']))
        except AccountConfigurationError:
            self._config_error = True

    def status(self):
        if self._config_error:
            return {'state': 'CONFIG_ERROR'}
        if self.settings.gmail_accounts_path is None:
            return self.workers[0].status()
        accounts = {worker.account_id: worker.status()['state'] if worker.settings.gmail_enabled else 'DISABLED'
                    for worker in self.workers}
        active = [worker.status()['state'] for worker in self.workers if worker.settings.gmail_enabled]
        state = ('DISABLED' if not self.settings.gmail_enabled or not active else
                 'READY' if all(value == 'READY' for value in active) else 'ATTENTION_REQUIRED')
        errors = {worker.account_id: worker.status()['last_action_error'] for worker in self.workers
                  if worker.settings.gmail_enabled and worker.status()['last_action_error']}
        return {'state': state, 'accounts': accounts, 'action_errors': errors}

    def summary_status(self):
        if self.settings.gmail_accounts_path is None and self.workers:
            return self.workers[0].summary_status()
        # Do not silently multiply a single-account external-send quota.
        return {'state': 'DISABLED_MULTI_ACCOUNT', 'source': 'subject_and_preview'}

    async def handle_classification(self, interaction, action, *, target_id=None, expected_revision=None):
        if not self.settings.gmail_enabled or not self.settings.gmail_actions_enabled or not self.settings.discord_authorized(
                guild_id=interaction.guild_id, channel_id=interaction.channel_id, user_id=interaction.user.id):
            await interaction.response.send_message('この操作は許可されていません。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target_id = target_id or interaction.message.id
        for worker in self.workers:
            if worker.engine is None or worker.client is None or not worker.settings.gmail_enabled:
                continue
            async with worker._operation_lock:
                details = await asyncio.to_thread(worker.engine.reaction_details, target_id,
                                                 interaction.guild_id, interaction.channel_id)
                if details is None:
                    continue
                if expected_revision is not None and expected_revision != await asyncio.to_thread(worker.engine.triage_revision, target_id):
                    await interaction.followup.send('このボタンは以前の分類のものです。/mail_history から最新の状態を確認してください。', ephemeral=True)
                    return
                if action == 'task' and (not worker.settings.gmail_tasks_enabled or not worker.settings.gmail_task_archive_enabled):
                    await interaction.followup.send('タスク登録が無効です。', ephemeral=True)
                    return
                if action == 'trash' and not worker.settings.gmail_trash_enabled:
                    await interaction.followup.send('ゴミ箱への移動が無効です。', ephemeral=True)
                    return
                try:
                    from .mail_triage import classify
                    before = await asyncio.to_thread(worker.engine.triage_record, target_id)
                    details.update(guild_id=interaction.guild_id, channel_id=interaction.channel_id)
                    result = await asyncio.to_thread(classify, worker.engine, worker.client, worker.task_store,
                                                     details, target_id, interaction.user.id, action)
                    if action != 'undo':
                        await asyncio.to_thread(worker.engine.queue_cleanup, target_id)
                        from .mail_history import cleanup
                        removed = await cleanup(worker, interaction.client)
                        record = await asyncio.to_thread(worker.engine.triage_record, target_id)
                        if record and record['state'] == 'DONE':
                            if str(target_id) in removed:
                                result += '\n元のメール通知をDiscordから削除しました。'
                            else:
                                result += '\n分類は完了しています。Discordの通知削除は再試行待ちです。'
                    elif before and before['state'] != 'UNDONE' and (await asyncio.to_thread(worker.engine.triage_record, target_id) or {}).get('state') == 'UNDONE':
                        from .mail_history import restore_notice
                        try:
                            await restore_notice(worker, interaction, details)
                        except Exception:
                            result += ' 通知の再表示に失敗しました。/mail_review から再表示できます。'

                except Exception:
                    result = '処理を完了できませんでした。同じボタンで再試行できます。取り消す場合は「元に戻す」を押してください。'
                import discord
                from .mail_history import short
                subject = short(discord, details.get('subject') or '（件名なし）', 100)
                view = None
                record = await asyncio.to_thread(worker.engine.triage_record, target_id)
                revision = await asyncio.to_thread(worker.engine.triage_revision, target_id)
                if action != 'undo' and record and record['state'] != 'UNDONE' and revision is not None:
                    from .mail_history import quick_undo_view
                    view = quick_undo_view(discord, self, target_id, interaction.user.id, revision)
                    result += '\n直後なら下のボタンで取り消せます。10分経過後やBot再起動後は /mail_history を使ってください。'
                await interaction.followup.send('対象メール：**' + subject + '**\n' + result,
                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                    **({'view': view} if view is not None else {}))
                return
        await interaction.followup.send('このメールを確認できません。Botの接続後に再試行してください。', ephemeral=True)

    async def handle_reaction(self, bot, payload):
        if not self.settings.gmail_enabled:
            return
        for worker in self.workers:
            # Each worker checks its own persisted message/guild/channel binding.
            await worker.handle_reaction(bot, payload)

    async def run(self, bot, discord):
        if not self.settings.gmail_enabled or self._config_error:
            return
        jobs = []
        for worker in self.workers:
            if worker.settings.gmail_enabled:
                if self.settings.gmail_accounts_path is not None:
                    worker.settings = replace(worker.settings, gemini_enabled=False)
                jobs.append(asyncio.create_task(worker.run(bot, discord)))
        try:
            if jobs:
                await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
