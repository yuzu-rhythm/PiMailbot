import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mailbot.config import Settings
from mailbot.gmail_notifier import GmailNotifier
from mailbot.gmail_sync import GmailSync
from mailbot.storage import SqliteStore


class Mail:
    can_modify = False
    def get_profile(self): return {'historyId': '100'}
    def list_history(self, *args, **kwargs):
        return {'historyId': '101', 'history': [{'messagesAdded': [{'message': {'id': 'mail1'}}]}]}
    def get_message(self, *args, **kwargs):
        return {'labelIds': ['INBOX'], 'payload': {'headers': [{'name': 'Subject', 'value': 'デバッグ用の架空メール'}]}}
    def apply_action(self, *args): raise AssertionError('Task creation must not mutate Gmail')


class MailTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = SqliteStore(root / 'tasks.sqlite3')
        self.engine = GmailSync(root / 'gmail.sqlite3')
        self.mail = Mail()
        self.engine.poll_once(self.mail)
        self.engine.poll_once(self.mail)
        self.engine.mark_sent('mail1', 99, guild_id=1, channel_id=2)
        self.settings = Settings(discord_token='synthetic', gmail_enabled=True,
            gmail_tasks_enabled=True, allowed_guild_ids=frozenset({1}),
            allowed_channel_ids=frozenset({2}), allowed_user_ids=frozenset({3}))
        self.notifier = GmailNotifier(self.settings, task_store=self.store)
        self.notifier.engine, self.notifier.client = self.engine, self.mail
        self.acks = []
        async def ack(emoji): self.acks.append(emoji)
        self.channel = SimpleNamespace(get_partial_message=lambda _: SimpleNamespace(add_reaction=ack))
        self.bot = SimpleNamespace(user=SimpleNamespace(id=4), get_channel=lambda _: self.channel)
        self.payload = SimpleNamespace(emoji='📝', guild_id=1, channel_id=2, user_id=3, message_id=99)

    def tearDown(self):
        self.engine.close()
        self.store.close()
        self.tmp.cleanup()

    def react(self): asyncio.run(self.notifier.handle_reaction(self.bot, self.payload))

    def test_registers_without_modify_scope_and_acknowledges(self):
        self.react()
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['title'], 'デバッグ用の架空メール')
        self.assertIsNone(tasks[0]['due_at'])
        self.assertEqual(self.acks, ['📋'])
        self.assertTrue(self.engine.action_done(99, 'task'))

    def test_notification_seeds_task_reaction_and_persists_exact_binding(self):
        self.engine.close()
        self.engine = GmailSync(Path(self.tmp.name) / 'new-gmail.sqlite3')
        self.engine.poll_once(self.mail)
        self.notifier.engine = self.engine
        self.channel.id, self.channel.guild = 2, SimpleNamespace(id=1)
        posts = []
        async def seed(emoji): self.acks.append(emoji)
        async def send(text, **kwargs):
            posts.append(text)
            return SimpleNamespace(id=99, add_reaction=seed)
        self.channel.send = send
        discord = SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: None))
        asyncio.run(self.notifier.cycle(self.bot, discord))
        self.assertIn('📝 タスクにする', posts[0])
        self.assertEqual(self.acks, ['📝'])
        self.assertEqual(self.engine.reaction_details(99, 1, 2)['message_id'], 'mail1')
        self.assertIsNone(self.engine.reaction_details(99, 1, 9))
        self.react()
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_repeat_and_restart_do_not_duplicate(self):
        self.react()
        self.react()
        self.engine.close()
        self.store.close()
        root = Path(self.tmp.name)
        self.engine = GmailSync(root / 'gmail.sqlite3')
        self.store = SqliteStore(root / 'tasks.sqlite3')
        self.notifier.engine, self.notifier.task_store = self.engine, self.store
        self.react()
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_recovery_between_task_commit_and_receipt_is_idempotent(self):
        with patch.object(self.engine, 'complete_action', side_effect=RuntimeError('synthetic receipt failure')):
            self.react()
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertFalse(self.engine.action_done(99, 'task'))
        self.react()
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertTrue(self.engine.action_done(99, 'task'))

    def test_wrong_user_guild_channel_unknown_message_and_bot_are_denied(self):
        for name, value in [('user_id', 5), ('guild_id', 9), ('channel_id', 8), ('message_id', 98), ('user_id', 4)]:
            with self.subTest(name=name, value=value):
                original = getattr(self.payload, name)
                setattr(self.payload, name, value)
                self.react()
                setattr(self.payload, name, original)
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.acks, [])

    def test_disabled_feature_does_not_register(self):
        self.notifier.settings = replace(self.settings, gmail_tasks_enabled=False)
        self.react()
        self.assertEqual(self.store.list_tasks(), [])

    def test_failed_storage_is_not_recorded_as_success(self):
        with patch.object(self.store, 'add_mail_task', side_effect=RuntimeError('synthetic failure')):
            self.react()
        self.assertFalse(self.engine.action_done(99, 'task'))
        self.assertEqual(self.acks, [])
        self.react()
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_ack_failure_keeps_task_and_retry_does_not_duplicate(self):
        async def fail(_): raise RuntimeError('synthetic acknowledgement failure')
        self.channel.get_partial_message = lambda _: SimpleNamespace(add_reaction=fail)
        self.react()
        self.assertEqual(self.notifier.status()['last_action_error'], 'ACTION_APPLIED_ACK_FAILED')
        self.react()
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_title_normalization_empty_and_long_subjects(self):
        for identity, subject, expected in [('empty', '\n\x00', 'メールを確認する（件名なし）'),
                                             ('long', 'x' * 700, 'x' * 500)]:
            task = self.store.add_mail_task(subject, guild_id=1, channel_id=2, message_id=identity)
            self.assertEqual(task['title'], expected)

    def test_same_subject_different_mail_and_completed_task(self):
        a = self.store.add_mail_task('same', guild_id=1, channel_id=2, message_id='a')
        b = self.store.add_mail_task('same', guild_id=1, channel_id=2, message_id='b')
        self.assertNotEqual(a['id'], b['id'])
        self.store.complete_task(a['id'])
        again = self.store.add_mail_task('same', guild_id=1, channel_id=2, message_id='a')
        self.assertTrue(again['completed'])
        self.assertEqual(len(self.store.list_tasks()), 2)


if __name__ == '__main__':
    unittest.main()
