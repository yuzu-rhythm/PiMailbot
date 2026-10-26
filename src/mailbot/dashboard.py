"""Read-only, loopback-only desk display. No third-party frontend dependencies."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import argparse
import copy
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlencode

from .calendar_client import CalendarClient, normalized_event
from .gmail_client import GmailClient
from .todoist_client import TodoistClient

JST = timezone(timedelta(hours=9))
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/timeline.js': ('timeline.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


def private_json(path):
    path = Path(path)
    if (path.is_symlink() or path.stat().st_mode & 0o077 or
            path.parent.stat().st_mode & 0o077 or path.stat().st_size > 65536):
        raise ValueError('PRIVATE_CONFIG_REQUIRED')
    return json.loads(path.read_text())


class Dashboard:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.state = {}
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.stop = threading.Event()
        for row in config.get('mailboxes', []):
            key = 'mail:' + row['id']
            self.state[key] = {'kind': 'mail', 'label': row['label'], 'state': 'LOADING'}
            self.jobs[key] = lambda row=row: self.mail(dict(row, triage_since=config.get('mail_triage_since')))
        self.state['tasks'] = {'kind': 'tasks', 'label': 'Todoist', 'state': 'NOT_CONFIGURED'}
        if config.get('todoist_token_path'):
            self.state['tasks']['state'] = 'LOADING'
            self.jobs['tasks'] = self.tasks
        self.state['calendar'] = {'kind': 'calendar', 'label': 'Google Calendar', 'state': 'NOT_CONFIGURED',
                                  'selected': [{'key': row['key'], 'label': row['label']} for row in config.get('calendars', [])]}
        if config.get('calendar_token_path'):
            self.state['calendar']['state'] = 'LOADING'
            self.jobs['calendar'] = self.calendars
        self.state['weather'] = {'kind': 'weather', 'label': '天気', 'state': 'NOT_CONFIGURED'}
        self.state['finance'] = {'kind': 'finance', 'label': '今日の支出', 'state': 'NOT_CONFIGURED'}

    @staticmethod
    def mail(row):
        client = GmailClient(Path(row['token_path']), timeout=10)
        profile = client.get_profile()
        if str(profile.get('emailAddress', '')).casefold() != row['email'].casefold():
            raise ValueError('IDENTITY_MISMATCH')
        result = client._get_json('/labels/INBOX')
        unread = result.get('messagesUnread')
        if type(unread) is not int or unread < 0:
            raise ValueError('INVALID_RESPONSE')
        result = {'unread': unread}
        since = row.get('triage_since')
        if since is not None:
            if type(since) is not int or since < 0:
                raise ValueError('INVALID_TRIAGE_START')
            query = f'in:inbox {{after:{since} label:"見直し対象" label:"Mailbot/見直し対象"}} -label:"処理済み" -label:"Mailbot/処理済み"'
            count, cursor = 0, None
            for _ in range(10):
                page = client.list_messages(query=query, page_token=cursor, max_results=100)
                messages = page.get('messages', [])
                if not isinstance(messages, list):
                    raise ValueError('INVALID_RESPONSE')
                count += len(messages)
                cursor = page.get('nextPageToken')
                if not cursor:
                    break
            result.update(unprocessed=count, unprocessed_partial=bool(cursor))
        return result

    def tasks(self):
        client = TodoistClient(Path(self.config['todoist_token_path']))
        rows, cursor = [], None
        for _ in range(5):
            params = {'limit': 100}
            if cursor:
                params['cursor'] = cursor
            result = client._request('/tasks?' + urlencode(params))
            items = result.get('results')
            if not isinstance(items, list):
                raise ValueError('INVALID_RESPONSE')
            for row in items:
                if row.get('checked') or row.get('is_completed'):
                    continue
                due = row.get('due') or {}
                value = due.get('datetime') or due.get('date')
                # Convert timed deadlines to JST before comparing day boundaries.
                if value and 'T' in value:
                    value = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(JST).isoformat()
                rows.append({'title': str(row.get('content', ''))[:500], 'due': value,
                             'priority': int(row.get('priority', 1))})
            cursor = result.get('next_cursor')
            if not cursor:
                break
        rows.sort(key=lambda row: (row['due'] is None, row['due'] or '', -row['priority']))
        return {'items': rows[:100], 'count': len(rows), 'partial': bool(cursor) or len(rows) > 100}

    def calendars(self):
        selected = self.config.get('calendars', [])
        if not selected or len(selected) > 10 or len({row['key'] for row in selected}) != len(selected):
            raise ValueError('Invalid calendar selection')
        client = CalendarClient(Path(self.config['calendar_token_path']), timeout=10)
        client.check_selection(self.config['calendar_account_email'], selected)
        start = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        items, partial = [], False
        for selection in selected:
            rows, truncated = client.events(selection['id'], start, end)
            partial = partial or truncated
            for row in rows:
                item = normalized_event(row, selection, JST)
                if item:
                    items.append(item)
        items.sort(key=lambda item: (item['start'], item['calendar'], item['title']))
        return {'items': items[:500], 'partial': partial or len(items) > 500,
                'range_start': start.isoformat(), 'range_end': end.isoformat()}

    def refresh(self, key, job):
        try:
            data = job()
            with self.lock:
                self.state[key].update(data, state='READY', updated_at=time.time())
        except Exception:
            # Do not publish exception strings, credentials, raw API responses or paths.
            with self.lock:
                item = self.state[key]
                item['state'] = 'STALE' if item.get('updated_at') else 'ERROR'
        finally:
            with self.lock:
                self.state[key]['attempted_at'] = time.time()

    def collect(self):
        while not self.stop.is_set():
            futures = [self.pool.submit(self.refresh, key, job) for key, job in self.jobs.items()]
            for future in futures:
                future.result()
            self.stop.wait(90)

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        now = time.time()
        for item in result.values():
            if item.get('updated_at') and now - item['updated_at'] > 240:
                item['state'] = 'STALE'
        return {'sources': result, 'server_time': now, 'timezone': 'Asia/Tokyo'}


def serve(config, port=8765):
    dashboard = Dashboard(config)
    host = '127.0.0.1'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            # Host checking also rejects DNS rebinding to the loopback service.
            if self.headers.get('Host') not in {f'{host}:{port}', f'localhost:{port}'}:
                self.send_error(403)
                return
            if self.path == '/api/snapshot':
                body = json.dumps(dashboard.snapshot(), ensure_ascii=False).encode()
                mime = 'application/json; charset=utf-8'
            elif self.path in ASSETS:
                name, mime = ASSETS[self.path]
                body = files('mailbot').joinpath('dashboard_static', name).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=dashboard.collect, daemon=True).start()
    print(f'Dashboard: http://{host}:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.stop.set()
        server.server_close()
        dashboard.pool.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    try:
        serve(private_json(args.config), args.port)
    except Exception:
        print('Dashboard startup failed. Check private configuration and port availability.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
