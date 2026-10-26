"""Mailbox isolation and archive-after-Todoist behavior; no external requests."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mailbot.config import Settings
from mailbot.gmail_accounts import AccountConfigurationError, GmailAccounts, load_accounts
from mailbot.gmail_client import GmailClient, GmailError, GMAIL_MODIFY_SCOPE
from mailbot.gmail_notifier import GmailNotifier, notification_text
from mailbot.gmail_sync import GmailSync, GmailSyncError
from mailbot.storage import SqliteStore


class Mail:
    can_modify = True

    def __init__(self, email):
        self.email = email
        self.actions = []
        self.fail = False

    def get_profile(self):
        return {'emailAddress': self.email, 'historyId': '100'}

    def list_history(self, *args, **kwargs):
        return {'historyId': '101', 'history': [{'messagesAdded': [{'message': {'id': 'same-id'}}]}]}

    def get_message(self, *args, **kwargs):
        return {'labelIds': ['INBOX'], 'payload': {'headers': [{'name': 'Subject', 'value': 'fixture'}]}}

    def apply_action(self, message, action):
        if self.fail:
            raise GmailError('synthetic unavailable', status=503)
        self.actions.append((message, action))


class GmailAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.manifest = self.root / 'accounts.json'
        self.rows = [{'id': ident, 'email': ident+'@example.invalid', 'guild_id': 1,
                      'channel_id': channel, 'enabled': True,
                      'token_path': str(self.root/(ident+'.json')),
                      'database_path': str(self.root/(ident+'.sqlite3'))}
                     for ident, channel in [('first', 2), ('second', 5)]]
        self.settings = Settings(database_path=self.root/'tasks.sqlite3', discord_token='synthetic',
            gmail_enabled=True, gmail_actions_enabled=True, gmail_tasks_enabled=True,
            gmail_trash_enabled=True, gmail_task_archive_enabled=True,
            allowed_guild_ids=frozenset({1}), allowed_channel_ids=frozenset({2, 5}),
            allowed_user_ids=frozenset({3}), gmail_accounts_path=self.manifest)
        self.write_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({'version': 1, 'accounts': self.rows}))
        self.manifest.chmod(0o600)

    def test_manifest_routes_and_default_disabled_features(self):
        rows = load_accounts(self.manifest, self.settings)
        self.assertEqual([row['channel_id'] for row in rows], [2, 5])
        self.assertFalse(Settings().gmail_trash_enabled)
        self.assertFalse(Settings().gmail_task_archive_enabled)

    def test_duplicate_routes_paths_emails_and_protected_database_rejected(self):
        for key in ('id', 'email', 'channel_id', 'token_path', 'database_path'):
            original = self.rows[1][key]
            self.rows[1][key] = self.rows[0][key]
            self.write_manifest()
            with self.subTest(key=key), self.assertRaises(AccountConfigurationError):
                load_accounts(self.manifest, self.settings)
            self.rows[1][key] = original
        self.rows[1]['database_path'] = str(self.settings.database_path)
        self.write_manifest()
        with self.assertRaises(AccountConfigurationError):
            load_accounts(self.manifest, self.settings)

    def test_manifest_does_not_expand_discord_access(self):
        with self.assertRaises(AccountConfigurationError):
            load_accounts(self.manifest, replace(self.settings, allowed_channel_ids=frozenset({2})))
        self.manifest.chmod(0o644)
        with self.assertRaises(AccountConfigurationError):
            load_accounts(self.manifest, self.settings)

    def test_unsafe_parent_and_symlink_credentials_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(AccountConfigurationError):
            load_accounts(self.manifest, self.settings)
        self.root.chmod(0o700)
        (self.root/'first.json').symlink_to(self.root/'elsewhere.json')
        with self.assertRaises(AccountConfigurationError):
            load_accounts(self.manifest, self.settings)

    def test_wrong_authenticated_mailbox_never_opens_cursor(self):
        notifier = GmailNotifier(replace(self.settings, gmail_token_path=self.root/'first.json'),
                                 expected_email='first@example.invalid')
        with patch('mailbot.gmail_notifier.GmailClient', return_value=Mail('wrong@example.invalid')):
            self.assertFalse(notifier.initialize())
        self.assertEqual(notifier.status()['state'], 'ACCOUNT_MISMATCH')
        self.assertIsNone(notifier.engine)

    def test_db_binding_persists_and_refuses_different_mailbox(self):
        path = self.root/'bound.sqlite3'
        with GmailSync(path) as engine:
            engine.bind_account('first@example.invalid')
        with GmailSync(path) as engine:
            engine.bind_account('FIRST@example.invalid')
            with self.assertRaises(GmailSyncError):
                engine.bind_account('second@example.invalid')

    def worker(self, row, discord_id, store):
        mail = Mail(row['email'])
        worker = GmailNotifier(self.settings, task_store=store, account_id=row['id'],
                               expected_email=row['email'], channel_id=row['channel_id'])
        worker.engine = GmailSync(row['database_path'])
        worker.engine.bind_account(row['email'])
        worker.engine.poll_once(mail)
        worker.engine.poll_once(mail)
        worker.engine.mark_sent('same-id', discord_id, guild_id=1, channel_id=row['channel_id'])
        worker.client = mail
        self.addCleanup(worker.engine.close)
        return worker

    @staticmethod
    def bot():
        async def ack(emoji): pass
        channel = SimpleNamespace(get_partial_message=lambda _: SimpleNamespace(add_reaction=ack))
        return SimpleNamespace(user=SimpleNamespace(id=4), get_channel=lambda _: channel)

    @staticmethod
    def payload(emoji='📝', message=99, channel=2):
        return SimpleNamespace(emoji=emoji, guild_id=1, channel_id=channel, user_id=3, message_id=message)

    def test_reaction_cannot_cross_mailbox_even_with_same_gmail_id(self):
        with SqliteStore(self.settings.database_path) as store:
            manager = GmailAccounts(self.settings, task_store=store)
            first, second = [self.worker(row, ident, store) for row, ident in zip(self.rows, (99, 100))]
            manager.workers = [first, second]
            asyncio.run(manager.handle_reaction(self.bot(), self.payload('🗑️')))
            self.assertEqual(first.client.actions, [('same-id', 'trash')])
            self.assertEqual(second.client.actions, [])
            asyncio.run(manager.handle_reaction(self.bot(), self.payload('📥', message=100, channel=2)))
            self.assertEqual(second.client.actions, [])

    def test_task_archive_waits_for_persisted_todoist_identity_then_retries(self):
        with SqliteStore(self.settings.database_path) as store:
            store.configure_todoist(True)
            worker = self.worker(self.rows[0], 99, store)
            asyncio.run(worker.handle_reaction(self.bot(), self.payload()))
            task = store.list_tasks()[0]
            asyncio.run(worker._archive_registered_tasks(self.bot()))
            self.assertEqual(worker.client.actions, [])
            self.assertTrue(store.todoist_begin_create(task['id']))
            store.todoist_fail(task['id'], 'REQUEST_FAILED', ambiguous=True)
            asyncio.run(worker._archive_registered_tasks(self.bot()))
            self.assertEqual(worker.client.actions, [])
            # Reconciliation fixture: an actual successful create must persist its remote ID.
            with store._transaction() as db:
                db.execute("UPDATE todoist_outbox SET state='CREATING' WHERE task_id=?", (task['id'],))
            store.todoist_link(task['id'], 'remote-fixture')
            worker.client.fail = True
            asyncio.run(worker._archive_registered_tasks(self.bot()))
            self.assertEqual(len(worker.engine.task_archives()), 1)
            worker.client.fail = False
            asyncio.run(worker._archive_registered_tasks(self.bot()))
            self.assertEqual(worker.client.actions, [('same-id', 'archive')])
            self.assertEqual(worker.engine.task_archives(), [])
            asyncio.run(worker._archive_registered_tasks(self.bot()))
            self.assertEqual(len(worker.client.actions), 1)

    def test_archive_queue_survives_restart_and_respects_trash(self):
        with SqliteStore(self.settings.database_path) as store:
            store.configure_todoist(True)
            worker = self.worker(self.rows[0], 99, store)
            asyncio.run(worker.handle_reaction(self.bot(), self.payload()))
            task = store.list_tasks()[0]
            store.todoist_begin_create(task['id'])
            store.todoist_link(task['id'], 'remote-fixture')
            worker.engine.close()
            worker.engine = GmailSync(self.rows[0]['database_path'])
            self.addCleanup(worker.engine.close)
            worker.engine.complete_action(99, 'trash', 3)
            asyncio.run(worker._archive_registered_tasks(self.bot()))
            self.assertEqual(worker.client.actions, [])
            self.assertEqual(worker.engine.task_archives(), [])

    def test_trash_disabled_and_raw_empty_request_body(self):
        with SqliteStore(self.settings.database_path) as store:
            worker = self.worker(self.rows[0], 99, store)
            worker.settings = replace(self.settings, gmail_trash_enabled=False)
            asyncio.run(worker.handle_reaction(self.bot(), self.payload('🗑️')))
            self.assertEqual(worker.client.actions, [])
        credentials = {'client_id': 'synthetic', 'client_secret': 'synthetic',
            'refresh_token': 'synthetic', 'token_uri': 'https://oauth2.googleapis.com/token',
            'scopes': [GMAIL_MODIFY_SCOPE]}
        client = GmailClient(credentials)
        with patch.object(client, '_get_json', return_value={}) as request:
            client.apply_action('same-id', 'trash')
            request.assert_called_once_with('/messages/same-id/trash', body=b'')
        wire = client._request('/messages/same-id/trash', None, 'synthetic', b'')
        self.assertEqual(wire.get_method(), 'POST')
        self.assertEqual(wire.data, b'')

    def test_generated_link_selects_expected_mailbox(self):
        text = notification_text({'message_id': 'same-id'}, account_email='first@example.invalid')
        self.assertIn('authuser=first%40example.invalid#all/same-id', text)
        self.assertNotIn('/u/0/', text)


if __name__ == '__main__':
    unittest.main()
