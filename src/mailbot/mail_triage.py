"""Explicit, reversible mail classification; no automatic mail mutations."""
import json
from urllib.parse import quote
from .mail_label_names import compatible_ids

LABELS = {'task': '要対応', 'later': 'あとで読む', 'keep': '保管', 'trash': '不要'}
PROCESSED = '処理済み'
REVIEW = '見直し対象'
TITLES = {'task': '要対応', 'later': 'あとで読む', 'keep': '保管', 'trash': '不要'}


def label_ids(client):
    if not client.can_modify:
        raise ValueError('MODIFY_AUTH_REQUIRED')
    rows = client._get_json('/labels').get('labels', [])
    result = compatible_ids({r['name']: r['id'] for r in rows if isinstance(r, dict) and 'name' in r and 'id' in r})
    for name in [*LABELS.values(), PROCESSED]:
        if name not in result:
            created = client._get_json('/labels', body={'name': name, 'labelListVisibility': 'labelShow',
                                                       'messageListVisibility': 'show'})
            result[name] = created['id']
    return result


def modify(client, message_id, add, remove):
    if not client.can_modify:
        raise ValueError('MODIFY_AUTH_REQUIRED')
    return client._get_json('/messages/' + quote(message_id, safe='') + '/modify',
                            body={'addLabelIds': sorted(set(add)), 'removeLabelIds': sorted(set(remove))})


def classify(engine, client, task_store, details, discord_id, user_id, action):
    """Caller holds the account's operation lock. Journal precedes remote changes."""
    previous = engine.triage_record(discord_id)
    if action == 'undo':
        if not previous or previous['state'] == 'UNDONE':
            return '取り消す分類はありません。'
        before = json.loads(previous['before_labels'])
        managed = json.loads(previous['managed_labels'])
        engine.triage_state(discord_id, 'UNDOING')
        engine.cancel_task_archive(discord_id)
        current = client.get_message(details['message_id'])
        current_labels = set(current.get('labelIds', []))
        # Never change read/star state or unrelated labels.
        if 'TRASH' not in before and 'TRASH' in current_labels:
            client._get_json('/messages/' + quote(details['message_id'], safe='') + '/untrash', body=b'')
        modify(client, details['message_id'], set(before) & set(managed), set(managed) - set(before))
        if '__category_archive__' in json.loads(previous['label_map']):
            from .mail_categories import read_categories
            categories, _ = read_categories(client, details['message_id'])
            engine.save_content_categories(details['message_id'], sorted(categories))
        engine.triage_state(discord_id, 'UNDONE')
        suffix = ' 作成したTodoistタスクは残しています。不要ならTodoistで整理してください。' if previous['action'] == 'task' else ''
        return 'メールの分類と受信トレイの状態を元に戻しました。' + suffix
    if action not in LABELS:
        raise ValueError('INVALID_CLASSIFICATION')
    if previous and previous['state'] != 'UNDONE':
        if previous['action'] != action:
            return 'このメールは分類済みです。/mail_history でこのメールの「元に戻す」を選んでから、再表示された通知で分類し直してください。'
        if previous['state'] in {'DONE', 'WAIT_TASK'}:
            return 'すでに分類済みです。' if previous['state'] == 'DONE' else 'Todoistへの登録を待っています。'
        if previous['state'] == 'UNDOING':
            return '取り消しが途中です。「元に戻す」を再度押してください。'
        ids = compatible_ids(json.loads(previous['label_map']))
    else:
        ids = label_ids(client)
        before = client.get_message(details['message_id']).get('labelIds')
        if not isinstance(before, list):
            raise ValueError('INVALID_MESSAGE')
        managed = ['INBOX', 'TRASH', *[ids[n] for n in [*LABELS.values(), PROCESSED]]]
        engine.triage_begin(discord_id, action, before, managed, ids, user_id)
    if action == 'task':
        if task_store is None:
            raise ValueError('TASK_STORAGE_REQUIRED')
        task = task_store.add_mail_task(details['subject'], guild_id=details['guild_id'],
                                       channel_id=details['channel_id'], message_id=details['message_id'])
        engine.queue_task_archive(discord_id, details['message_id'], task['id'], user_id)
        engine.triage_state(discord_id, 'WAIT_TASK')
        return '要対応にしました。Todoistへの登録成功後にメールを保管し、通知を解除します。'
    modify(client, details['message_id'], [ids[LABELS[action]], ids[PROCESSED]],
           ['INBOX', *[ids[name] for key, name in LABELS.items() if key != action]])
    if action == 'trash':
        client.apply_action(details['message_id'], 'trash')
    engine.triage_state(discord_id, 'DONE')
    return TITLES[action] + 'に分類しました。取り消す場合は /mail_history でこのメールの「元に戻す」を選んでください。'


def finish_task(engine, client, discord_id, message_id):
    row = engine.triage_record(discord_id)
    if not row or row['state'] != 'WAIT_TASK':
        return
    ids = compatible_ids(json.loads(row['label_map']))
    modify(client, message_id, [ids[LABELS['task']], ids[PROCESSED]], ['INBOX'])
    engine.triage_state(discord_id, 'DONE')


def make_view(discord, accounts):
    """Stable IDs and one registered view keep buttons usable after restart."""
    view = discord.ui.View(timeout=None)
    emojis = {'task': '📝', 'later': '📖', 'keep': '📥', 'trash': '🗑️', 'undo': '↩️'}
    for action, label in [*TITLES.items(), ('undo', '元に戻す')]:
        button = discord.ui.Button(label=label, emoji=emojis[action], custom_id='mailbot:triage:' + action,
                                   style=discord.ButtonStyle.secondary)
        async def callback(interaction, selected=action):
            await accounts.handle_classification(interaction, selected)
        button.callback = callback
        view.add_item(button)
    category = discord.ui.Button(label='分類', emoji='🏷️', row=1,
        custom_id='mailbot:content_categories', style=discord.ButtonStyle.secondary)
    async def open_categories(interaction):
        from .mail_categories import handle
        await handle(accounts, interaction)
    category.callback = open_categories
    view.add_item(category)
    return view
