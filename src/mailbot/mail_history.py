"""Compact private processing history and durable cleanup of bot-owned notices."""
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from .mail_triage import TITLES, make_view

JST = timezone(timedelta(hours=9))


def quick_undo_view(discord, accounts, target_id, user_id, revision):
    """Private short-lived control bound to the exact classification generation."""
    view = discord.ui.View(timeout=600)
    button = discord.ui.Button(label='取り消す（10分以内）', emoji='↩️',
                               style=discord.ButtonStyle.secondary)

    async def undo(click):
        if click.user.id != user_id:
            await click.response.send_message('この操作は実行できません。', ephemeral=True)
            return
        await accounts.handle_classification(click, 'undo', target_id=target_id,
                                              expected_revision=revision)

    button.callback = undo
    view.add_item(button)
    return view


def short(discord, value, limit):
    value = ' '.join(str(value or '').split())
    if len(value) > limit:
        value = value[:limit-1] + '…'
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


async def cleanup(worker, bot):
    import discord
    removed = set()
    for row in await asyncio.to_thread(worker.engine.cleanup_targets):
        guild_id, channel_id = int(row['guild_id']), int(row['channel_id'])
        if guild_id not in worker.settings.allowed_guild_ids or channel_id not in worker.settings.allowed_channel_ids:
            continue
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            if getattr(getattr(channel, 'guild', None), 'id', None) != guild_id:
                continue
            try:
                message = await channel.fetch_message(int(row['discord_id']))
            except discord.NotFound:
                message = None
            if message is not None:
                if message.author.id != bot.user.id:
                    continue
                await message.delete()
            await asyncio.to_thread(worker.engine.finish_cleanup, row['discord_id'])
            removed.add(str(row['discord_id']))
        except Exception:
            # Gmail is already processed. Keep only notification removal pending.
            worker._action_error = 'NOTIFICATION_CLEANUP_PENDING'
    return removed


async def restore_notice(worker, interaction, details):
    import discord
    from .gmail_notifier import notification_text
    text = notification_text(details, account_email=worker.expected_email).replace('📩 新着メール', '📩 取り消したメール', 1)
    sent = await interaction.channel.send(text, view=make_view(discord, interaction.client.mail_accounts),
        allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
    await asyncio.to_thread(worker.engine.rebind_sent, details['message_id'], sent.id,
                           interaction.guild_id, interaction.channel_id)


async def show_history(accounts, interaction, discord, page=1):
    if not accounts.settings.gmail_enabled or not accounts.settings.discord_authorized(
            guild_id=interaction.guild_id, channel_id=interaction.channel_id, user_id=interaction.user.id):
        await interaction.response.send_message('この履歴は表示できません。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    worker = next((w for w in accounts.workers if w.settings.gmail_enabled and w.engine is not None and
                   (w.channel_id or w.settings.reminder_channel_id) == interaction.channel_id), None)
    if worker is None:
        await interaction.followup.send('このチャンネルの履歴を取得できません。', ephemeral=True)
        return
    page = max(1, min(int(page), 10000))
    rows, total = await asyncio.to_thread(worker.engine.history_page, interaction.guild_id, interaction.channel_id, page=page)
    pages = max(1, (total+4)//5)
    if page > pages:
        page = pages
        rows, total = await asyncio.to_thread(worker.engine.history_page, interaction.guild_id, interaction.channel_id, page=page)
    embed = discord.Embed(title='メールの処理履歴', description='保存済みのAI要約を優先し、なければ短いプレビューを表示します。')
    states = {'DONE':'処理済み', 'WAIT_TASK':'Todoist登録待ち', 'APPLYING':'処理途中', 'UNDOING':'取り消し途中', 'UNDONE':'取り消し済み'}
    view = discord.ui.View(timeout=600)
    for i, row in enumerate(rows, 1):
        when = datetime.fromtimestamp(row['updated_at'], JST).strftime('%m/%d %H:%M')
        label = TITLES.get(row['action'], '処理')
        preview = row.get('summary') or row.get('snippet') or 'プレビューなし'
        source = 'AI要約' if row.get('summary') else 'プレビュー'
        location = '?authuser='+quote(worker.expected_email, safe='') if worker.expected_email else 'u/0/'
        url = 'https://mail.google.com/mail/'+location+'#all/'+quote(row['message_id'], safe='')
        embed.add_field(name=f"{i}. {label}｜{short(discord, row['subject'] or '件名なし', 70)}",
            value=f"{source}：{short(discord, preview, 110)}\n{when} · {states.get(row['state'], '状態不明')} · [Gmailで開く]({url})", inline=False)
        if row['state'] != 'UNDONE':
            button = discord.ui.Button(label=f'{i}を元に戻す', emoji='↩️', row=0)
            async def undo(click, target=row['discord_id']):
                # Callback target is server-side, then exact delivery/actor checks run again.
                await accounts.handle_classification(click, 'undo', target_id=target)
            button.callback = undo
            view.add_item(button)
        category = discord.ui.Button(label=f'{i}の分類', emoji='🏷️', row=1)
        async def categorize(click, target=row['discord_id']):
            from .mail_categories import handle
            await handle(accounts, click, target_id=target)
        category.callback = categorize
        view.add_item(category)
    for text, target in [('前の5件', page-1), ('次の5件', page+1)]:
        button = discord.ui.Button(label=text, row=2, disabled=target<1 or target>pages)
        async def navigate(click, destination=target):
            await show_history(accounts, click, discord, destination)
        button.callback = navigate
        view.add_item(button)
    embed.set_footer(text=f'{page}/{pages}ページ · {total}件 · ボタンは10分間有効。/mail_historyで再表示できます')
    if not rows:
        embed.description = 'メールの処理履歴はまだありません。'
    await interaction.followup.send(embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
