"""Explicit multi-select content labels, independent of mail processing actions."""
import asyncio
import json

from .mail_triage import modify

CATEGORIES = {
    'school': ('学校', '🎓'), 'research': ('研究', '🔬'),
    'life': ('生活・手続き', '🏠'), 'money': ('お金', '💰'),
    'career': ('就活', '💼'), 'shopping': ('買い物・配送', '📦'),
    'news': ('広告・お知らせ', '📣'),
}
LABELS = {key: title for key, (title, _) in CATEGORIES.items()}
LEGACY_LABELS = {key: 'Mailbot/内容/' + title for key, (title, _) in CATEGORIES.items()}


class CategoryLabelConflict(ValueError):
    pass


def category_label_ids(client):
    labels = client._get_json('/labels').get('labels', [])
    ids = {row['name']: row['id'] for row in labels
           if isinstance(row, dict) and 'name' in row and 'id' in row}
    for key, name in LABELS.items():
        legacy = ids.get(LEGACY_LABELS[key])
        if legacy is not None:
            if name in ids and ids[name] != legacy:
                raise CategoryLabelConflict('CATEGORY_LABEL_CONFLICT')
            ids[name] = legacy
    return ids


def read_categories(client, message_id):
    ids = category_label_ids(client)
    present = set(client.get_message(message_id).get('labelIds', []))
    selected = frozenset(key for key, name in LABELS.items() if ids.get(name) in present)
    return selected, ids


def save_categories(worker, message_id, selected, expected, discord_id, user_id):
    """Journal content+archive together for unprocessed notices; preserve prior actions."""
    current, ids = read_categories(worker.client, message_id)
    previous = worker.engine.triage_record(discord_id)
    resume = bool(previous and previous['state'] == 'APPLYING' and
                  json.loads(previous['label_map']).get('__category_archive__') == sorted(selected))
    if previous and previous['state'] not in {'DONE', 'WAIT_TASK', 'UNDONE'} and not resume:
        raise ValueError('PROCESSING_INCOMPLETE')
    if current != expected and not resume:
        return False
    if not worker.client.can_modify:
        raise ValueError('MODIFY_AUTH_REQUIRED')
    for key in sorted(selected):
        name = LABELS[key]
        if name not in ids:
            row = worker.client._get_json('/labels', body={
                'name': name, 'labelListVisibility': 'labelShow',
                'messageListVisibility': 'show'})
            ids[name] = row['id']
    archive = resume or not previous or previous['state'] == 'UNDONE'
    add = [ids[LABELS[key]] for key in selected]
    remove = [ids[name] for key, name in LABELS.items() if key not in selected and name in ids]
    if archive:
        from .mail_triage import label_ids, LABELS as PROCESSING, PROCESSED
        if resume:
            journal_ids = json.loads(previous['label_map'])
        else:
            journal_ids = label_ids(worker.client)
            before = worker.client.get_message(message_id).get('labelIds')
            if not isinstance(before, list):
                raise ValueError('INVALID_MESSAGE')
            managed = {'INBOX', 'TRASH', *[journal_ids[n] for n in [*PROCESSING.values(), PROCESSED]],
                       *[ids[n] for n in LABELS.values() if n in ids]}
            journal_ids['__category_archive__'] = sorted(selected)
            worker.engine.triage_begin(discord_id, 'keep', before, sorted(managed), journal_ids, user_id)
        add += [journal_ids[PROCESSING['keep']], journal_ids[PROCESSED]]
        remove += ['INBOX', *[journal_ids[name] for key, name in PROCESSING.items() if key != 'keep']]
    if add or remove:
        modify(worker.client, message_id, add, remove)
    actual, _ = read_categories(worker.client, message_id)
    if actual != selected:
        raise ValueError('CATEGORY_SAVE_UNCONFIRMED')
    if archive:
        labels = set(worker.client.get_message(message_id).get('labelIds', []))
        if 'INBOX' in labels or journal_ids[PROCESSED] not in labels:
            raise ValueError('ARCHIVE_UNCONFIRMED')
        worker.engine.triage_state(discord_id, 'DONE')
    worker.engine.save_content_categories(message_id, sorted(selected))
    return 'ARCHIVED' if archive else 'UPDATED'


def selection_view(discord, accounts, interaction, target_id, current):
    class SelectionView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=600)
            self.selected = set(current)
            self.finished = False

        async def interaction_check(self, click):
            if (click.user.id != interaction.user.id or click.guild_id != interaction.guild_id
                    or click.channel_id != interaction.channel_id or self.finished):
                await click.response.send_message('この選択画面は使えません。「分類」から開き直してください。', ephemeral=True)
                return False
            return True

    view = SelectionView()
    select = discord.ui.Select(placeholder='内容を選択（複数可・すべて解除も可）',
        min_values=0, max_values=len(CATEGORIES), options=[
            discord.SelectOption(label=title, value=key, emoji=emoji, default=key in current)
            for key, (title, emoji) in CATEGORIES.items()])

    async def choose(click):
        view.selected = set(select.values)
        for option in select.options:
            option.default = option.value in view.selected
        await click.response.edit_message(view=view)

    select.callback = choose
    view.add_item(select)
    save = discord.ui.Button(label='分類して保管', style=discord.ButtonStyle.primary, row=1)

    async def commit(click):
        # Freeze this form before any network operation; errors require reopening.
        view.finished = True
        view.stop()
        await handle(accounts, click, target_id=target_id,
                     selected=frozenset(view.selected), expected=current)
        try:
            await interaction.edit_original_response(view=None)
        except Exception:
            pass

    save.callback = commit
    view.add_item(save)
    cancel = discord.ui.Button(label='キャンセル', row=1)

    async def abort(click):
        view.finished = True
        view.stop()
        await click.response.edit_message(content='分類の変更をキャンセルしました。', view=None)

    cancel.callback = abort
    view.add_item(cancel)
    return view


async def handle(accounts, interaction, *, target_id=None, selected=None, expected=None):
    import discord
    from .mail_history import short
    settings = accounts.settings
    if (not settings.gmail_enabled or not settings.gmail_actions_enabled or
            not settings.discord_authorized(guild_id=interaction.guild_id,
                channel_id=interaction.channel_id, user_id=interaction.user.id)):
        await interaction.response.send_message('この操作は許可されていません。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target_id = target_id or interaction.message.id
    for worker in accounts.workers:
        if worker.engine is None or worker.client is None or not worker.settings.gmail_enabled:
            continue
        async with worker._operation_lock:
            details = await asyncio.to_thread(worker.engine.reaction_details, target_id,
                interaction.guild_id, interaction.channel_id)
            if details is None:
                continue
            result_view = None
            try:
                subject = short(discord, details.get('subject') or '（件名なし）', 100)
                if selected is None:
                    current, _ = await asyncio.to_thread(read_categories, worker.client, details['message_id'])
                    view = selection_view(discord, accounts, interaction, target_id, current)
                    await interaction.followup.send('対象メール：**' + subject +
                        '**\n内容を複数選び「分類して保管」を押してください。すべて解除して保管することもできます。\n'
                        '未処理メールは受信トレイから外し、Discordの通知も削除します。既読・未読は変えません。\n'
                        'すでに要対応・あとで読む・保管・不要にしたメールは、内容の分類だけ更新します。選択画面は10分間有効です。',
                        view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                    return
                if not isinstance(selected, frozenset) or not selected <= CATEGORIES.keys() or expected is None:
                    raise ValueError('INVALID_SELECTION')
                saved = await asyncio.to_thread(save_categories, worker, details['message_id'], selected, expected,
                                                target_id, interaction.user.id)
                if not saved:
                    text = '別の操作で分類が変更されています。「分類」から開き直して確認してください。'
                else:
                    names = '・'.join(title for key, (title, _) in CATEGORIES.items() if key in selected) or '未分類'
                    text = '対象メール：**' + subject + '**\n内容の分類を保存しました：' + names
                    if saved == 'ARCHIVED':
                        await asyncio.to_thread(worker.engine.queue_cleanup, target_id)
                        from .mail_history import cleanup, quick_undo_view
                        removed = await cleanup(worker, interaction.client)
                        text += '\nGmailを保管し、受信トレイから外しました。'
                        text += '\nDiscordの元通知も削除しました。' if str(target_id) in removed else '\nDiscordの通知削除は再試行待ちです。'
                        revision = await asyncio.to_thread(worker.engine.triage_revision, target_id)
                        result_view = quick_undo_view(discord, accounts, target_id, interaction.user.id, revision)
                        text += '\n下のボタンで今回の分類と保管を取り消せます。10分後・Bot再起動後は /mail_history を使ってください。'
                    else:
                        text += '\nすでに整理済みのため、元の処理状態を維持しました。'
                        record = await asyncio.to_thread(worker.engine.triage_record, target_id)
                        if record and record['state'] == 'DONE':
                            from .mail_history import cleanup
                            await asyncio.to_thread(worker.engine.queue_cleanup, target_id)
                            await cleanup(worker, interaction.client)
            except CategoryLabelConflict:
                text = '同名の分類ラベルが別に存在するため、変更を止めました。既存ラベルは統合していません。'
            except Exception:
                text = '分類・保管の完了を確認できませんでした。通知は自動削除していません。「分類」から現在の状態を確認するか、/mail_history で「元に戻す」を選んでください。'
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                                            **({'view': result_view} if result_view is not None else {}))
            return
    await interaction.followup.send('このメールを確認できません。保存済みのメール通知から操作してください。', ephemeral=True)
