"""Durable Gmail history cursor and private Discord notification outbox."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
import re
import json
import os
import sqlite3
import threading
import time
from typing import Iterator, Protocol

from .gmail_client import GmailAuthError, GmailError, GmailTransientError, HistoryExpiredError


class GmailReadClient(Protocol):
    def get_profile(self) -> dict[str, object]: ...
    def list_history(self, start_history_id: str, *, page_token: str | None = None, max_results: int = 100) -> dict[str, object]: ...
    def get_message(self, message_id: str, *, format: str = "metadata") -> dict[str, object]: ...


class GmailSyncError(RuntimeError):
    pass


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
_META = frozenset(("history_id", "history_page_token", "last_successful_at", "state", "last_error", "account_email"))


def _id(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise GmailSyncError(f"invalid Gmail {name}")
    return value


def _text(value: object, limit: int = 4000) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _header(message: Mapping[str, object], wanted: str) -> str:
    payload = message.get("payload")
    headers = payload.get("headers") if isinstance(payload, Mapping) else None
    if not isinstance(headers, list):
        return ""
    for item in headers:
        if isinstance(item, Mapping) and str(item.get("name", "")).casefold() == wanted.casefold():
            return _text(item.get("value"))
    return ""


def _received(message: Mapping[str, object]) -> int | None:
    try:
        value = int(message.get("internalDate"))
    except (TypeError, ValueError, OverflowError):
        return None
    return value // 1000 if value >= 0 else None


class GmailSync:
    def __init__(self, database_path: str | Path, *, page_limit: int = 10) -> None:
        if not 1 <= page_limit <= 100:
            raise ValueError("page_limit must be between 1 and 100")
        self.database_path, self._page_limit = Path(database_path), page_limit
        if self.database_path.is_symlink() or self.database_path.parent.is_symlink():
            raise GmailSyncError('Gmail state path must not be a symlink')
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.database_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        self._connection = sqlite3.connect(self.database_path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._lock, self._closed = threading.RLock(), False
        self._schema()
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def __enter__(self) -> "GmailSync":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def bind_account(self, email: str) -> None:
        """A cursor/outbox must never be reused with a different mailbox."""
        normalized = email.strip().casefold()
        if not normalized or '@' not in normalized:
            raise GmailSyncError('Invalid account identity')
        with self._lock, self._transaction() as db:
            existing = self._meta('account_email')
            if existing is not None and existing != normalized:
                raise GmailSyncError('ACCOUNT_IDENTITY_MISMATCH')
            self._set(db, 'account_email', normalized)

    def queue_task_archive(self, discord_id, message_id, task_id, user_id):
        with self._lock, self._transaction() as db:
            db.execute("INSERT INTO gmail_task_archives(discord_id,message_id,task_id,user_id,state) VALUES(?,?,?,?,'PENDING') ON CONFLICT(discord_id) DO UPDATE SET state='PENDING',last_error=NULL",
                       (str(discord_id), _id(message_id, 'message ID'), task_id, str(user_id)))

    def task_archives(self, limit=5):
        with self._lock:
            self._open()
            return [dict(row) for row in self._connection.execute(
                "SELECT a.*,d.guild_id,d.channel_id FROM gmail_task_archives a JOIN gmail_deliveries d ON d.discord_id=a.discord_id AND d.message_id=a.message_id WHERE a.state='PENDING' ORDER BY a.discord_id LIMIT ?", (limit,)).fetchall()]

    def finish_task_archive(self, discord_id, *, error=None):
        with self._lock, self._transaction() as db:
            db.execute('UPDATE gmail_task_archives SET state=?,last_error=? WHERE discord_id=?',
                       ('PENDING' if error else 'DONE', error, str(discord_id)))

    def start(self, client: GmailReadClient) -> dict[str, object]:
        return self._baseline(client, force=False)

    def reset_baseline(self, client: GmailReadClient) -> dict[str, object]:
        """Explicit operator action for an expired history cursor."""
        return self._baseline(client, force=True)

    def _baseline(self, client: GmailReadClient, *, force: bool) -> dict[str, object]:
        with self._lock:
            self._open()
            existing = self._meta("history_id")
            if existing and not force:
                return self._result("ALREADY_STARTED", history_id=existing)
            try:
                profile = client.get_profile()
                history_id = _id(profile.get("historyId"), "history ID")
                with self._transaction() as db:
                    self._set(db, "history_id", history_id)
                    self._set(db, "history_page_token", "")
                    self._set(db, "last_successful_at", str(int(time.time())))
                    self._set(db, "state", "READY")
                    self._set(db, "last_error", "")
                return self._result("BASELINED", history_id=history_id)
            except GmailError as exc:
                self._record_provider(exc)
                raise

    def poll_once(self, client: GmailReadClient) -> dict[str, object]:
        with self._lock:
            self._open()
            if not self._meta("history_id"):
                try:
                    return self.start(client)
                except GmailError as exc:
                    return self._error_result(exc)
            try:
                return self._poll_history(client)
            except HistoryExpiredError as exc:
                self._record_state("RESYNC_REQUIRED", "Gmail history cursor expired")
                return self._error_result(exc, "RESYNC_REQUIRED")
            except GmailAuthError as exc:
                self._record_state("AUTH_REQUIRED", "Gmail authorization required")
                return self._error_result(exc, "AUTH_REQUIRED")
            except GmailTransientError as exc:
                self._record_state("BACKOFF", "Gmail service is temporarily unavailable")
                return self._error_result(exc, "BACKOFF")
            except GmailError as exc:
                if exc.status == 404:
                    self._record_state("RESYNC_REQUIRED", "Gmail history cursor expired")
                    return self._error_result(exc, "RESYNC_REQUIRED")
                self._record_state("ERROR", "Gmail request failed")
                return self._error_result(exc, "ERROR")

    def pending(self, limit: int = 5) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        with self._lock:
            self._open()
            rows = self._connection.execute(
                "SELECT message_id, sender, subject, snippet, received_at, created_at, status, discord_message_id "
                "FROM gmail_notifications WHERE status='PENDING' ORDER BY created_at, message_id LIMIT ?",
                (min(limit, 1000),),
            ).fetchall()
        return [dict(row) | {"id": row["message_id"]} for row in rows]

    def mark_sent(self, message_id: str, discord_message_id: str | int, *, guild_id: int | None = None, channel_id: int | None = None) -> bool:
        message_id = _id(message_id, "message ID")
        if discord_message_id is None:
            raise ValueError("Discord message ID is required")
        discord_id = _text(str(discord_message_id), 256)
        if not discord_id:
            raise ValueError("Discord message ID is required")
        with self._lock, self._transaction() as db:
            changed = db.execute(
                "UPDATE gmail_notifications SET status='SENT', discord_message_id=?, sent_at=? "
                "WHERE message_id=? AND status='PENDING'",
                (discord_id, int(time.time()), message_id),
            ).rowcount
            if changed and guild_id is not None and channel_id is not None:
                db.execute("INSERT OR IGNORE INTO gmail_deliveries VALUES(?,?,?,?)",
                           (discord_id, message_id, str(guild_id), str(channel_id)))
        return bool(changed)

    def reserve_summary(self, message_id: str, daily_limit: int) -> dict[str, object]:
        """Reserve quota before network I/O. Interrupted requests are not replayed."""
        message_id = _id(message_id, 'message ID')
        if not 1 <= daily_limit <= 100:
            raise ValueError('invalid summary quota')
        day = time.strftime('%Y-%m-%d', time.gmtime())
        with self._lock, self._transaction() as db:
            row = db.execute('SELECT state,summary,next_action FROM gmail_summaries WHERE message_id=?', (message_id,)).fetchone()
            if row:
                return dict(row) | {'reserved': False}
            used = db.execute("SELECT COUNT(*) FROM gmail_summaries WHERE day=? AND state!='QUOTA_LIMIT'", (day,)).fetchone()[0]
            state = 'RESERVED' if used < daily_limit else 'QUOTA_LIMIT'
            db.execute('INSERT INTO gmail_summaries VALUES(?,?,?,?,?,?)', (message_id, day, state, None, None, int(time.time())))
            return {'reserved': state == 'RESERVED', 'state': state, 'summary': None, 'next_action': None}

    def finish_summary(self, message_id: str, result: dict[str, str] | None) -> None:
        with self._lock, self._transaction() as db:
            db.execute("UPDATE gmail_summaries SET state=?,summary=?,next_action=? WHERE message_id=? AND state='RESERVED'",
                       ('DONE' if result else 'FAILED', result['summary'] if result else None,
                        result['next_action'] if result else None, message_id))

    def reaction_target(self, discord_id: int, guild_id: int, channel_id: int) -> str | None:
        row = self.reaction_details(discord_id, guild_id, channel_id)
        return str(row['message_id']) if row else None

    def reaction_details(self, discord_id: int, guild_id: int, channel_id: int) -> dict[str, object] | None:
        with self._lock:
            self._open()
            row = self._connection.execute(
                "SELECT d.message_id,n.subject,n.sender,n.snippet FROM gmail_deliveries d JOIN gmail_notifications n ON n.message_id=d.message_id "
                "WHERE d.discord_id=? AND d.guild_id=? AND d.channel_id=? AND n.status='SENT'",
                (str(discord_id), str(guild_id), str(channel_id))).fetchone()
            return dict(row) if row else None

    def action_done(self, discord_id: int, action: str) -> bool:
        with self._lock:
            self._open()
            return self._connection.execute("SELECT 1 FROM gmail_actions WHERE discord_id=? AND action=?",
                                            (str(discord_id), action)).fetchone() is not None

    def complete_action(self, discord_id: int, action: str, user_id: int) -> None:
        with self._lock, self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO gmail_actions VALUES(?,?,?,?)",
                       (str(discord_id), action, str(user_id), int(time.time())))

    def bind_review_delivery(self, message_id, discord_id, guild_id, channel_id):
        with self._lock, self._transaction() as db:
            db.execute('INSERT OR IGNORE INTO gmail_deliveries VALUES(?,?,?,?)',
                       (str(discord_id), _id(message_id, 'message ID'), str(guild_id), str(channel_id)))

    def offer_review(self, message):
        message_id = _id(message.get('id'), 'message ID')
        with self._lock, self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO gmail_notifications(message_id,sender,subject,snippet,received_at,created_at,status) VALUES(?,?,?,?,?,?,'PENDING')",
                       (message_id, _header(message,'From'), _header(message,'Subject'), _text(message.get('snippet')),
                        _received(message), int(time.time())))
            return dict(db.execute('SELECT *,discord_message_id AS discord_id FROM gmail_notifications WHERE message_id=?', (message_id,)).fetchone())

    def triage_record(self, discord_id):
        with self._lock:
            row = self._connection.execute('SELECT * FROM gmail_triage WHERE discord_id=?', (str(discord_id),)).fetchone()
            return dict(row) if row else None

    def triage_revision(self, discord_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(id) FROM gmail_triage_audit WHERE discord_id=? AND state='APPLYING'",
                (str(discord_id),)).fetchone()
            return row[0] if row else None

    def save_content_categories(self, message_id, categories):
        with self._lock, self._transaction() as db:
            db.execute('INSERT OR REPLACE INTO gmail_content_categories VALUES(?,?,?)',
                       (message_id, json.dumps(categories), int(time.time())))

    def triage_begin(self, discord_id, action, before, managed, label_map, user_id):
        with self._lock, self._transaction() as db:
            db.execute('INSERT OR REPLACE INTO gmail_triage VALUES(?,?,?,?,?,?,?,?,?)',
                       (str(discord_id), action, json.dumps(before), json.dumps(managed),
                        json.dumps(label_map), str(user_id), 'APPLYING', int(time.time()), int(time.time())))
            db.execute('INSERT INTO gmail_triage_audit(discord_id,action,state,user_id,created_at) VALUES(?,?,?,?,?)',
                       (str(discord_id), action, 'APPLYING', str(user_id), int(time.time())))

    def triage_state(self, discord_id, state):
        with self._lock, self._transaction() as db:
            db.execute('UPDATE gmail_triage SET state=?,updated_at=? WHERE discord_id=?',
                       (state, int(time.time()), str(discord_id)))
            db.execute('INSERT INTO gmail_triage_audit(discord_id,action,state,user_id,created_at) SELECT discord_id,action,?,user_id,? FROM gmail_triage WHERE discord_id=?',
                       (state, int(time.time()), str(discord_id)))

    def cancel_task_archive(self, discord_id):
        with self._lock, self._transaction() as db:
            db.execute("UPDATE gmail_task_archives SET state='CANCELLED' WHERE discord_id=?", (str(discord_id),))
            db.execute("UPDATE gmail_notification_cleanup SET state='CANCELLED' WHERE discord_id=?", (str(discord_id),))

    def history_page(self, guild_id, channel_id, *, page=1, size=5):
        # Exact account/location binding prevents cross-channel disclosure.
        with self._lock:
            total = self._connection.execute("SELECT COUNT(*) FROM gmail_triage t JOIN gmail_deliveries d USING(discord_id) WHERE d.guild_id=? AND d.channel_id=?", (str(guild_id), str(channel_id))).fetchone()[0]
            rows = self._connection.execute(
                "SELECT t.discord_id,t.action,t.state,t.updated_at,d.message_id,n.subject,n.snippet,s.summary "
                "FROM gmail_triage t JOIN gmail_deliveries d USING(discord_id) JOIN gmail_notifications n USING(message_id) "
                "LEFT JOIN gmail_summaries s ON s.message_id=d.message_id AND s.state='DONE' "
                "WHERE d.guild_id=? AND d.channel_id=? ORDER BY t.updated_at DESC,t.discord_id DESC LIMIT ? OFFSET ?",
                (str(guild_id), str(channel_id), size, (page-1)*size)).fetchall()
            return [dict(row) for row in rows], total

    def rebind_sent(self, message_id, discord_id, guild_id, channel_id):
        with self._lock, self._transaction() as db:
            db.execute('UPDATE gmail_notifications SET discord_message_id=? WHERE message_id=? AND status=\'SENT\'',
                       (str(discord_id), message_id))
            db.execute('INSERT OR IGNORE INTO gmail_deliveries VALUES(?,?,?,?)',
                       (str(discord_id), message_id, str(guild_id), str(channel_id)))

    def queue_cleanup(self, discord_id):
        with self._lock, self._transaction() as db:
            db.execute("INSERT INTO gmail_notification_cleanup(discord_id,state) SELECT discord_id,'PENDING' FROM gmail_triage WHERE discord_id=? AND state='DONE' ON CONFLICT(discord_id) DO UPDATE SET state='PENDING'", (str(discord_id),))

    def cleanup_targets(self):
        with self._lock:
            return [dict(row) for row in self._connection.execute(
                "SELECT d.* FROM gmail_notification_cleanup c JOIN gmail_triage t USING(discord_id) JOIN gmail_deliveries d USING(discord_id) WHERE c.state='PENDING' AND t.state='DONE' LIMIT 5").fetchall()]

    def finish_cleanup(self, discord_id):
        with self._lock, self._transaction() as db:
            db.execute("UPDATE gmail_notification_cleanup SET state='DONE' WHERE discord_id=?", (str(discord_id),))

    def status(self) -> dict[str, object]:
        with self._lock:
            self._open()
            rows = self._connection.execute("SELECT status, COUNT(*) AS n FROM gmail_notifications GROUP BY status").fetchall()
            counts = {str(row["status"]).lower(): int(row["n"]) for row in rows}
            return {
                "state": self._meta("state") or "BASELINE_REQUIRED",
                "history_id": self._meta("history_id"),
                "history_page_pending": bool(self._meta("history_page_token")),
                "last_successful_at": self._int_meta("last_successful_at"),
                "last_error": self._meta("last_error") or None,
                "pending_count": counts.get("pending", 0),
                "sent_count": counts.get("sent", 0),
            }

    def _poll_history(self, client: GmailReadClient) -> dict[str, object]:
        history_id = self._meta("history_id")
        if not history_id:
            raise GmailSyncError("history cursor is not initialized")
        page_token, pages, discovered, inserted = self._meta("history_page_token") or None, 0, 0, 0
        seen: set[str] = set()
        while pages < self._page_limit:
            response = client.list_history(history_id, page_token=page_token, max_results=100)
            if not isinstance(response, Mapping):
                raise GmailSyncError("Gmail history response is invalid")
            ids: list[str] = []
            for entry in response.get("history", []) or []:
                if not isinstance(entry, Mapping):
                    continue
                for added in entry.get("messagesAdded", []) or []:
                    message = added.get("message") if isinstance(added, Mapping) else None
                    message_id = message.get("id") if isinstance(message, Mapping) else None
                    if isinstance(message_id, str) and message_id not in seen:
                        _id(message_id, "message ID")
                        seen.add(message_id)
                        ids.append(message_id)
            notifications: list[dict[str, object]] = []
            for message_id in ids:
                if self._exists(message_id):
                    continue
                try:
                    message = client.get_message(message_id, format="metadata")
                except GmailError as exc:
                    if exc.status == 404:  # deleted between history and fetch
                        continue
                    raise
                if not isinstance(message, Mapping) or not isinstance(message.get("labelIds"), list):
                    raise GmailSyncError("Gmail message response is invalid")
                if "INBOX" not in message["labelIds"]:
                    continue
                notifications.append({
                    "message_id": message_id,
                    "sender": _header(message, "From"),
                    "subject": _header(message, "Subject"),
                    "snippet": _text(message.get("snippet")),
                    "received_at": _received(message),
                    "created_at": int(time.time()),
                })
            next_token = response.get("nextPageToken")
            if next_token is not None and not isinstance(next_token, str):
                raise GmailSyncError("Gmail history page token is invalid")
            current = response.get("historyId")
            next_history = history_id if not isinstance(current, str) or not current else _id(current, "history ID")
            if not next_token and (not isinstance(current, str) or not current):
                raise GmailSyncError("Gmail history response has no history ID")
            with self._transaction() as db:
                for item in notifications:
                    db.execute(
                        "INSERT OR IGNORE INTO gmail_notifications(message_id,sender,subject,snippet,received_at,created_at,status) VALUES(?,?,?,?,?,?, 'PENDING')",
                        (item["message_id"], item["sender"], item["subject"], item["snippet"], item["received_at"], item["created_at"]),
                    )
                    inserted += int(db.execute("SELECT changes()").fetchone()[0])
                if next_token:
                    self._set(db, "history_page_token", next_token)
                else:
                    self._set(db, "history_id", next_history)
                    self._set(db, "history_page_token", "")
                self._set(db, "last_successful_at", str(int(time.time())))
                self._set(db, "state", "READY")
                self._set(db, "last_error", "")
            pages, discovered = pages + 1, discovered + len(ids)
            if not next_token:
                return self._result("READY", pages=pages, discovered=discovered, inserted=inserted, history_id=next_history)
            page_token = next_token
        return self._result("PARTIAL", pages=pages, discovered=discovered, inserted=inserted, history_id=history_id)

    def _schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                "CREATE TABLE IF NOT EXISTS gmail_content_categories(message_id TEXT PRIMARY KEY,categories TEXT NOT NULL,updated_at INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS gmail_notification_cleanup(discord_id TEXT PRIMARY KEY,state TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS gmail_triage(discord_id TEXT PRIMARY KEY,action TEXT NOT NULL,before_labels TEXT NOT NULL,managed_labels TEXT NOT NULL,label_map TEXT NOT NULL,user_id TEXT NOT NULL,state TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS gmail_triage_audit(id INTEGER PRIMARY KEY,discord_id TEXT NOT NULL,action TEXT NOT NULL,state TEXT NOT NULL,user_id TEXT NOT NULL,created_at INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS gmail_task_archives(discord_id TEXT PRIMARY KEY,message_id TEXT NOT NULL,task_id TEXT NOT NULL,user_id TEXT NOT NULL,state TEXT NOT NULL,last_error TEXT);"
                "CREATE TABLE IF NOT EXISTS gmail_summaries(message_id TEXT PRIMARY KEY,day TEXT NOT NULL,state TEXT NOT NULL,summary TEXT,next_action TEXT,created_at INTEGER NOT NULL);"
                "CREATE INDEX IF NOT EXISTS gmail_summaries_day ON gmail_summaries(day);"
                "CREATE TABLE IF NOT EXISTS gmail_deliveries(discord_id TEXT PRIMARY KEY,message_id TEXT NOT NULL,guild_id TEXT NOT NULL,channel_id TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS gmail_actions(discord_id TEXT NOT NULL,action TEXT NOT NULL,user_id TEXT NOT NULL,completed_at INTEGER NOT NULL,PRIMARY KEY(discord_id,action));"
                "CREATE TABLE IF NOT EXISTS gmail_sync_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS gmail_notifications(message_id TEXT PRIMARY KEY,sender TEXT NOT NULL,subject TEXT NOT NULL,snippet TEXT NOT NULL,received_at INTEGER,created_at INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('PENDING','SENT')),discord_message_id TEXT,sent_at INTEGER);"
            )
            self._set(self._connection, "state", "BASELINE_REQUIRED")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _exists(self, message_id: str) -> bool:
        return self._connection.execute("SELECT 1 FROM gmail_notifications WHERE message_id=?", (message_id,)).fetchone() is not None

    def _meta(self, key: str) -> str | None:
        if key not in _META:
            raise KeyError(key)
        row = self._connection.execute("SELECT value FROM gmail_sync_meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _int_meta(self, key: str) -> int | None:
        value = self._meta(key)
        try:
            return int(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def _set(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute("INSERT INTO gmail_sync_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _record_state(self, state: str, error: str) -> None:
        with self._connection:
            self._set(self._connection, "state", state)
            self._set(self._connection, "last_error", error)

    def _record_provider(self, error: GmailError) -> None:
        if isinstance(error, GmailAuthError):
            state, text = "AUTH_REQUIRED", "Gmail authorization required"
        elif isinstance(error, GmailTransientError):
            state, text = "BACKOFF", "Gmail service is temporarily unavailable"
        elif isinstance(error, HistoryExpiredError):
            state, text = "RESYNC_REQUIRED", "Gmail history cursor expired"
        else:
            state, text = "ERROR", "Gmail request failed"
        self._record_state(state, text)

    def _error_result(self, error: GmailError, state: str | None = None) -> dict[str, object]:
        state = state or getattr(error, "state", "ERROR")
        if state == "AUTH_REQUIRED":
            text = "Gmail authorization required"
        elif state == "BACKOFF":
            text = "Gmail service is temporarily unavailable"
        elif state == "RESYNC_REQUIRED":
            text = "Gmail history cursor expired"
        else:
            text = "Gmail request failed"
        return self._result(state, error=text, retry_after=getattr(error, "retry_after", None))

    def _result(self, state: str, **extra: object) -> dict[str, object]:
        result = {"state": state, "pending": self._pending_count()}
        result.update(extra)
        return result

    def _pending_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM gmail_notifications WHERE status='PENDING'").fetchone()[0])

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("GmailSync is closed")
