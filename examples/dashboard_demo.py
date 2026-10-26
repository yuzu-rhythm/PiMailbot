#!/usr/bin/env python3
"""Serve the actual dashboard assets with fictional data and no API clients."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import time

HOST, PORT = '127.0.0.1', 8877
JST = timezone(timedelta(hours=9))
ASSETS = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'),
          '/app.js': ('app.js', 'text/javascript'), '/timeline.js': ('timeline.js', 'text/javascript')}
DEMO_CLOCK = b"""'use strict';
const NativeDate = Date;
const parts = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new NativeDate());
const part = name => parts.find(x => x.type===name).value;
const demoTime = new NativeDate(part('year')+'-'+part('month')+'-'+part('day')+'T10:15:00+09:00').getTime();
globalThis.Date = class extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [demoTime])); }
  static now() { return demoTime; }
};
"""


def snapshot():
    today = datetime.now(JST).date()
    day = today.isoformat()
    stamp = datetime.fromisoformat(day+'T10:15:00+09:00').timestamp()
    def event(title, calendar, start, end, offset=0):
        date = (today + timedelta(days=offset)).isoformat()
        return {'title': title, 'calendar': calendar, 'all_day': False,
                'start': date+'T'+start+':00+09:00', 'end': date+'T'+end+':00+09:00'}
    sources = {
        'tasks': {'kind': 'tasks', 'state': 'READY', 'updated_at': stamp, 'count': 4,
                  'items': [{'title': '輪読会の資料を確認する', 'priority': 3, 'due': day},
                            {'title': '応募書類の下書きを見直す', 'priority': 2, 'due': day},
                            {'title': '来週の予定を整理する', 'priority': 1, 'due': None},
                            {'title': '消耗品を買う', 'priority': 1, 'due': None}]},
        'calendar': {'kind': 'calendar', 'state': 'READY', 'updated_at': stamp,
                     'selected': [{'key': 'personal', 'label': '個人'}, {'key': 'lab', 'label': '研究室'}],
                     'items': [event('資料を読む', 'personal', '09:00', '10:00'),
                               event('輪読会', 'lab', '10:30', '12:00'),
                               event('実験の準備', 'lab', '13:30', '15:00'),
                               event('進捗をまとめる', 'personal', '15:30', '16:30'),
                               event('ミーティング', 'lab', '14:00', '15:00', 2)]}}
    for key, label, unread in [('school', 'school@example.invalid', 2),
                               ('personal', 'personal@example.invalid', 1),
                               ('research', 'research@example.invalid', 0)]:
        sources['mail:'+key] = {'kind': 'mail', 'label': label, 'unread': unread,
                               'unprocessed': 0, 'state': 'READY', 'updated_at': stamp}
    return {'sources': sources, 'timezone': 'Asia/Tokyo', 'server_time': stamp}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get('Host') not in {f'{HOST}:{PORT}', f'localhost:{PORT}'}:
            self.send_error(403)
            return
        if self.path == '/api/snapshot':
            body, mime = json.dumps(snapshot(), ensure_ascii=False).encode(), 'application/json'
        elif self.path == '/demo-clock.js':
            body, mime = DEMO_CLOCK, 'text/javascript'
        elif self.path in ASSETS:
            name, mime = ASSETS[self.path]
            body = files('mailbot').joinpath('dashboard_static', name).read_bytes()
            if name == 'index.html':
                body = body.replace(b'<title>', '<title>架空データのデモ — '.encode())
                body = body.replace(b'<head>', b'<head><script src="/demo-clock.js"></script>')
                body = body.replace(b'<footer>', '<footer><span>架空データのデモ・表示時刻10:15固定・外部API接続なし</span>'.encode())
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime+'; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'架空データのデモ: http://{HOST}:{PORT}/ — 外部API接続なし', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
