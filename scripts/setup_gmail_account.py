#!/usr/bin/env python3
"""Authorize exactly one configured mailbox without replacing other credentials."""
import argparse
import json
from pathlib import Path
import sys

from setup_gmail import (ROOT, GmailSetupError, _authorize, _atomic_write,
                         _private_parent, _read_client, _verify_target)

sys.path.insert(0, str(ROOT / 'src'))
from mailbot.config import Settings
from mailbot.gmail_accounts import AccountConfigurationError, load_accounts
from mailbot.gmail_client import GmailClient, GmailError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True, help='account id from private accounts.json')
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    manifest = ROOT / 'data/google/accounts.json'
    try:
        # Validate the private manifest before allowing any credential writes.
        document = json.loads(manifest.read_text())
        rows = document.get('accounts', [])
        settings = Settings(allowed_guild_ids=frozenset(row['guild_id'] for row in rows),
                            allowed_channel_ids=frozenset(row['channel_id'] for row in rows))
        accounts = load_accounts(manifest, settings)
        row = next((row for row in accounts if row['id'] == args.account), None)
        if row is None:
            raise GmailSetupError('Account id not found')
        target = _verify_target(ROOT, row['token_path'].relative_to(ROOT))
        if target.exists() and not args.replace:
            raise GmailSetupError('Account already authorized; use --replace only if intended')
        client = _verify_target(ROOT, Path('data/google/client_secret.json'))
        _read_client(client)
        _private_parent(ROOT)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        payload = _authorize(client, modify=True, login_hint=row['email'])
        credentials = json.loads(payload)
        profile = GmailClient(credentials).get_profile()
        if str(profile.get('emailAddress', '')).casefold() != row['email']:
            raise GmailSetupError('Wrong Google account selected; credentials were not saved')
        _atomic_write(target, payload)
        for account in document['accounts']:
            if account['id'] == row['id']:
                account['enabled'] = True
        _verify_target(ROOT, Path('data/google/accounts.json'))
        _atomic_write(manifest, (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode())
    except KeyboardInterrupt:
        return 130
    except (GmailSetupError, AccountConfigurationError, GmailError, OSError, ValueError, KeyError, TypeError):
        print('Account authorization incomplete. No other mailbox was changed; retry after checking the selected Google account and test-user access.', file=sys.stderr)
        return 2
    print('Mailbox identity verified and token saved privately. Pi settings are unchanged.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
