#!/usr/bin/env python3
"""Authorize Calendar separately from Gmail. Do not print OAuth URLs or secrets."""
import argparse
import json
from pathlib import Path
import sys

from setup_gmail import ROOT, _verify_target, _private_parent, _atomic_write, _read_client, TOKEN_URI
sys.path.insert(0, str(ROOT / 'src'))
from mailbot.calendar_client import CalendarClient, CALENDAR_SCOPES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        token = _verify_target(ROOT, Path('data/google/calendar/token.json'))
        pending = _verify_target(ROOT, Path('data/google/calendar/pending-token.json'))
        selection_path = _verify_target(ROOT, Path('data/google/calendar/selection.json'))
        _private_parent(ROOT)
        token.parent.mkdir(mode=0o700, exist_ok=True)
        if token.parent.is_symlink() or token.parent.stat().st_mode & 0o077:
            raise ValueError('Private directory required')
        if token.exists() and not args.replace:
            print('Calendar credentials already exist; use --replace only to authorize again.')
            return 2
        selection = json.loads(selection_path.read_text())
        client = ROOT / 'data/google/client_secret.json'
        _read_client(client)
        if pending.exists() and not args.replace:
            raw = pending.read_bytes()
            data = json.loads(raw)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client), list(CALENDAR_SCOPES))
            print('Googleの認証画面で、研究室カレンダーが見える大学アカウントを選び、読み取りを許可してください。', flush=True)
            credentials = flow.run_local_server(host='localhost', bind_addr='127.0.0.1', port=0,
                timeout_seconds=600, access_type='offline', prompt='consent',
                authorization_prompt_message='', login_hint=selection['account_email'],
                success_message='Calendar authorization completed. You may close this window.')
            raw = credentials.to_json().encode()
            data = json.loads(raw)
            if data.get('token_uri') != TOKEN_URI or not data.get('refresh_token'):
                raise ValueError('Reusable credentials required')
            CalendarClient(data)
            _atomic_write(pending, raw)
        check = CalendarClient(data)
        check.check_selection(selection['account_email'], selection['calendars'])
        _atomic_write(token, raw)
        pending.unlink(missing_ok=True)
        print('CALENDAR_AUTHORIZED: 選択した3カレンダーの読み取り認証を保存しました。')
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('Calendar authorization incomplete. Check account, consent and Google Calendar API enablement. Secret details are hidden.', file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
