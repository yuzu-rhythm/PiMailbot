"""On-demand historical review, at most ten inbox messages per invocation."""
import asyncio
from datetime import datetime, timezone, timedelta
from .gmail_notifier import notification_text
from .mail_triage import REVIEW, make_view, modify
from .mail_label_names import compatible_ids

JST = timezone(timedelta(hours=9))


async def review(accounts, interaction, discord, since, load=False):
    if not accounts.settings.gmail_enabled:
        await interaction.followup.send('Gmail連携が無効です。', ephemeral=True)
        return
    try:
        start = datetime.strptime(since, '%Y-%m-%d').replace(tzinfo=JST)
        if start > datetime.now(JST):
            raise ValueError()
    except ValueError:
        await interaction.followup.send('開始日は YYYY-MM-DD で、今日以前の日付を指定してください。', ephemeral=True)
        return
    worker = next((w for w in accounts.workers if
                   (w.channel_id or w.settings.reminder_channel_id) == interaction.channel_id and
                   w.engine is not None and w.client is not None and w.settings.gmail_enabled), None)
    if worker is None:
        await interaction.followup.send('このチャンネルのGmail接続を確認できません。', ephemeral=True)
        return
    query = f'in:inbox after:{int(start.timestamp())} -label:"処理済み" -label:"Mailbot/処理済み"'
    async with worker._operation_lock:
        try:
            page = await asyncio.to_thread(worker.client.list_messages, query=query, max_results=10)
            rows = page.get('messages', [])
            if not load:
                estimate = page.get('resultSizeEstimate', len(rows))
                await interaction.followup.send(
                    f'{since}以降の受信トレイが対象です。Gmailの概算件数: {estimate}件。'
                    '\n既読も含みます。送信済み・アーカイブ済み・迷惑メール・ゴミ箱は対象外です。'
                    '\nload を true にすると、最大10件をこのチャンネルへ表示します。表示後に分類してから再実行してください。', ephemeral=True)
                return
            if not worker.settings.gmail_actions_enabled or not worker.client.can_modify:
                await interaction.followup.send('メール整理の権限が必要です。', ephemeral=True)
                return
            # Explicit load opts only this bounded batch into dashboard review.
            labels = await asyncio.to_thread(worker.client._get_json, '/labels')
            ids = compatible_ids({r['name']: r['id'] for r in labels.get('labels', [])})
            review_id = ids.get(REVIEW)
            if review_id is None:
                created = await asyncio.to_thread(worker.client._get_json, '/labels', body={'name': REVIEW})
                review_id = created['id']
            sent_count, existing = 0, []
            for row in rows[:10]:
                message = await asyncio.to_thread(worker.client.get_message, row['id'])
                if 'INBOX' not in message.get('labelIds', []):
                    continue
                item = await asyncio.to_thread(worker.engine.offer_review, message)
                await asyncio.to_thread(modify, worker.client, row['id'], [review_id], [])
                if item['status'] == 'SENT':
                    try:
                        original = await interaction.channel.fetch_message(int(item['discord_id']))
                    except discord.NotFound:
                        original = None
                    if original is not None:
                        if original.author.id != interaction.client.user.id:
                            raise ValueError('DELIVERY_MISMATCH')
                        await original.edit(view=make_view(discord, accounts))
                        await asyncio.to_thread(worker.engine.bind_review_delivery, item['message_id'], item['discord_id'], interaction.guild_id, interaction.channel_id)
                        existing.append('https://discord.com/channels/' + str(interaction.guild_id) + '/' +
                                        str(interaction.channel_id) + '/' + str(item['discord_id']))
                        continue
                text = notification_text(item, account_email=worker.expected_email).replace('📩 新着メール', '📩 過去メールの見直し', 1)
                sent = await interaction.channel.send(text, view=make_view(discord, accounts),
                    allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
                if item['status'] == 'SENT':
                    await asyncio.to_thread(worker.engine.rebind_sent, item['message_id'], sent.id, interaction.guild_id, interaction.channel_id)
                else:
                    await asyncio.to_thread(worker.engine.mark_sent, item['message_id'], sent.id,
                                            guild_id=interaction.guild_id, channel_id=interaction.channel_id)
                sent_count += 1
            message = f'{sent_count}件を表示しました。分類すると、次の実行時に残りのメールを表示します。'
            if existing:
                message += '\nすでに通知済みのメールはこちらで分類してください:\n' + '\n'.join(existing)
            await interaction.followup.send(message, ephemeral=True, suppress_embeds=True)
        except Exception:
            await interaction.followup.send('見直しの読み込みを完了できませんでした。再実行できます。すでに表示されたメールの分類状態も確認してください。', ephemeral=True)
