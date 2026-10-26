"""Approved one-off rename of existing content labels, preserving label IDs."""
import argparse
import json
import os
from pathlib import Path
import shlex

from .config import Settings
from .gmail_accounts import load_accounts
from .gmail_client import GmailClient
from .mail_categories import LABELS, LEGACY_LABELS, CategoryLabelConflict
from .mail_label_names import LEGACY


def plan_accounts():
    env = {}
    for line in Path('/etc/mailbot/mailbot.env').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and not key.strip().startswith('#'):
            parts = shlex.split(value, comments=True)
            env[key.strip()] = parts[0] if parts else ''
    env['MAILBOT_DATABASE_PATH'] = '/var/lib/mailbot/mailbot.sqlite3'
    settings = Settings.from_env(env)
    if settings.gmail_accounts_path is None:
        raise ValueError('ACCOUNT_MANIFEST_REQUIRED')
    clients, plans = {}, []
    for row in load_accounts(settings.gmail_accounts_path, settings):
        if not row['enabled']:
            continue
        client = GmailClient(row['token_path'])
        if client.get_profile().get('emailAddress', '').casefold() != row['email'].casefold():
            raise ValueError('ACCOUNT_MISMATCH')
        ids = {r['name']: r['id'] for r in client._get_json('/labels').get('labels', [])}
        pairs = [(LEGACY_LABELS[key], new) for key, new in LABELS.items()]
        pairs.extend((old, new) for new, old in LEGACY.items())
        for old, new in pairs:
            if old not in ids:
                continue
            if new in ids and ids[new] != ids[old]:
                raise CategoryLabelConflict('CATEGORY_LABEL_CONFLICT')
            plans.append({'account': row['id'], 'id': ids[old], 'old': old, 'new': new})
        clients[row['id']] = client
    return clients, plans


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--journal', type=Path)
    args = parser.parse_args()
    try:
        # Read-only preflight across all accounts before the first rename.
        clients, plans = plan_accounts()
        print('Existing managed labels to rename:', len(plans))
        if not args.apply:
            return 0
        if args.journal is None:
            raise ValueError('JOURNAL_REQUIRED')
        fd = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as journal:
            def record(value):
                journal.write(json.dumps(value, ensure_ascii=False) + '\n')
                journal.flush()
                os.fsync(journal.fileno())
            record({'plan': plans})
            for plan in plans:
                client = clients[plan['account']]
                ids = {r['name']: r['id'] for r in client._get_json('/labels').get('labels', [])}
                if ids.get(plan['old']) != plan['id'] or plan['new'] in ids:
                    raise CategoryLabelConflict('CATEGORY_LABEL_CHANGED')
                record({'attempt': plan})
                result = client.rename_label(plan['id'], plan['new'])
                if result.get('id') != plan['id'] or result.get('name') != plan['new']:
                    raise ValueError('RENAME_UNCONFIRMED')
                record({'done': plan})
        print('Renamed managed labels:', len(plans))
        return 0
    except CategoryLabelConflict:
        print('Stopped: conflicting label name. No labels were merged. Check private migration journal if present.')
    except Exception:
        print('Rename incomplete or unconfirmed. New Bot supports both names. Check private migration journal; no automatic retry.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
