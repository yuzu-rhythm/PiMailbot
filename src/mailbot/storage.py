"""Small, durable SQLite store for local mailbot state.

The store is intentionally self contained.  No network client, Discord
library, or third party package is needed to use it.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Iterator, Mapping
import uuid


class StorageError(RuntimeError):
    """Raised for a local persistence failure or invalid local input."""


def utc_timestamp(value: datetime | date | int | float | str | None) -> int | None:
    """Convert common due-date inputs to a UTC Unix timestamp.

    Naive datetimes and ISO strings are interpreted as UTC so a local CLI run
    is deterministic.  Strings may end in ``Z`` or contain an explicit offset.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("due date must be ISO-8601, for example 2026-10-23T09:00:00Z") from exc
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.astimezone(timezone.utc).timestamp())
    raise TypeError(f"unsupported timestamp type: {type(value).__name__}")


def utc_now() -> int:
    return int(time.time())


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _SAFE_ID.fullmatch(task_id):
        raise ValueError("task ID must contain only ASCII letters, digits, '.', '_' or '-'")
    return task_id


def _validate_text(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} is too long")
    return normalized


class SqliteStore:
    """Thread-safe SQLite store with WAL and restart-safe reminder state."""

    SCHEMA_VERSION = 1

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self._todoist_enabled = False
        self.database_path = Path(database_path).expanduser()
        if str(self.database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.database_path),
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA busy_timeout = 10000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if str(self.database_path) != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = NORMAL")
            self._migrate()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None  # type: ignore[assignment]

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            try:
                self._connection.execute("BEGIN")
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _migrate(self) -> None:
        assert self._connection is not None
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        current = int(row["version"])
        if current < 1:
            self._connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    due_at INTEGER,
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    reminder_sent_at INTEGER,
                    CHECK (length(title) BETWEEN 1 AND 500)
                );
                CREATE INDEX tasks_due_idx
                    ON tasks(completed_at, due_at, reminder_sent_at);

                CREATE TABLE game_credits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,
                    credits INTEGER NOT NULL CHECK (credits >= 0),
                    note TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX game_credits_game_idx
                    ON game_credits(game, created_at);

                CREATE TABLE images (
                    id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL UNIQUE,
                    stored_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    size INTEGER NOT NULL CHECK (size > 0),
                    content_type TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX images_created_idx ON images(created_at);

                INSERT INTO schema_migrations(version, applied_at)
                VALUES (1, strftime('%s', 'now'));
                """
            )
        if current > self.SCHEMA_VERSION:
            raise StorageError("database schema is newer than this mailbot")
        self._connection.commit()

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "title": row["title"],
            "due_at": row["due_at"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "reminder_sent_at": row["reminder_sent_at"],
            "completed": row["completed_at"] is not None,
        }

    def add_task(
        self,
        title: str,
        *,
        due_at: datetime | date | int | float | str | None = None,
        task_id: str | None = None,
    ) -> dict[str, object]:
        title = _validate_text(title, field_name="title", max_length=500)
        parsed_due = utc_timestamp(due_at)
        task_id = _validate_task_id(task_id) if task_id else uuid.uuid4().hex
        created_at = utc_now()
        with self._transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO tasks(id, title, due_at, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, title, parsed_due, created_at),
                )
                if self._todoist_enabled:
                    self._queue_todoist(connection, task_id)
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"task ID already exists: {task_id}") from exc
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict[str, object] | None:
        task_id = _validate_task_id(task_id)
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            row = self._connection.execute(
                "SELECT id, title, due_at, created_at, completed_at, reminder_sent_at "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def add_mail_task(self, subject: str, *, guild_id: int, channel_id: int,
                      message_id: str) -> dict[str, object]:
        """Create one local task per mail/location, including after a restart.

        The deterministic identity makes recovery safe if the Gmail action
        receipt could not be written after this transaction committed.
        """
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,256}', message_id):
            raise ValueError('invalid mail identity')
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
               for value in (guild_id, channel_id)):
            raise ValueError('invalid mail location')
        if not isinstance(subject, str):
            raise ValueError('invalid mail subject')
        title = ' '.join(''.join(c if c.isprintable() else ' ' for c in subject).split())[:500]
        title = title or 'メールを確認する（件名なし）'
        identity = f'mailbot:gmail:{guild_id}:{channel_id}:{message_id}'
        task_id = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        with self._transaction() as connection:
            inserted = connection.execute(
                'INSERT INTO tasks(id,title,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING',
                (task_id, title, utc_now())).rowcount
            if inserted and self._todoist_enabled:
                self._queue_todoist(connection, task_id)
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            return self._task_from_row(row)

    def list_tasks(self, *, include_completed: bool = True) -> list[dict[str, object]]:
        where = "" if include_completed else "WHERE completed_at IS NULL"
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            rows = self._connection.execute(
                "SELECT id, title, due_at, created_at, completed_at, reminder_sent_at "
                f"FROM tasks {where} ORDER BY completed_at IS NOT NULL, "
                "due_at IS NULL, due_at, created_at, id"
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def complete_task(self, task_id: str, *, completed_at: int | None = None) -> dict[str, object]:
        task_id = _validate_task_id(task_id)
        completed_at = utc_now() if completed_at is None else utc_timestamp(completed_at)
        assert completed_at is not None
        with self._transaction() as connection:
            result = connection.execute(
                "UPDATE tasks SET completed_at = ? WHERE id = ? AND completed_at IS NULL",
                (completed_at, task_id),
            )
            if result.rowcount == 0:
                existing = connection.execute(
                    "SELECT id FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(f"unknown task: {task_id}")
            if self._todoist_enabled:
                connection.execute("UPDATE todoist_outbox SET state='PENDING',retry_at=0 WHERE task_id=? AND remote_id IS NOT NULL AND remote_completed=0",
                                   (task_id,))
        return self.get_task(task_id)  # type: ignore[return-value]

    def configure_todoist(self, enabled: bool) -> None:
        """An additive integration table keeps earlier releases rollback-compatible."""
        if enabled:
            with self._transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS todoist_outbox('
                    'task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,'
                    'request_id TEXT NOT NULL,remote_id TEXT UNIQUE,state TEXT NOT NULL,'
                    'remote_completed INTEGER NOT NULL DEFAULT 0,last_error TEXT,'
                    'retry_at INTEGER NOT NULL DEFAULT 0,last_checked INTEGER NOT NULL DEFAULT 0)')
                db.execute("CREATE INDEX IF NOT EXISTS todoist_outbox_state ON todoist_outbox(state,retry_at,last_checked)")
                db.execute("UPDATE todoist_outbox SET state='UNCERTAIN',last_error='INTERRUPTED_CREATE' WHERE state='CREATING'")
                db.execute("UPDATE todoist_outbox SET state='PENDING',retry_at=0 WHERE state='AUTH_REQUIRED'")
                db.execute("UPDATE todoist_outbox SET state='PENDING' WHERE remote_id IS NOT NULL AND remote_completed=0 AND task_id IN (SELECT id FROM tasks WHERE completed_at IS NOT NULL) AND state='SYNCED'")
        self._todoist_enabled = enabled

    @staticmethod
    def _queue_todoist(db, task_id):
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'mailbot:todoist:create:' + task_id))
        db.execute("INSERT OR IGNORE INTO todoist_outbox(task_id,request_id,state) VALUES(?,?,'PENDING')", (task_id, request_id))

    def todoist_pending(self, limit=3):
        with self._lock:
            rows = self._connection.execute(
                "SELECT t.*,o.remote_id,o.request_id FROM todoist_outbox o JOIN tasks t ON t.id=o.task_id "
                "WHERE o.state='PENDING' AND o.retry_at<=? ORDER BY t.created_at,t.id LIMIT ?", (utc_now(), limit)).fetchall()
            return [dict(row) for row in rows]

    def todoist_begin_create(self, task_id):
        with self._transaction() as db:
            changed = db.execute("UPDATE todoist_outbox SET state='CREATING' WHERE task_id=? AND state='PENDING' AND remote_id IS NULL", (task_id,)).rowcount
            return bool(changed)

    def todoist_link(self, task_id, remote_id):
        with self._transaction() as db:
            db.execute("UPDATE todoist_outbox SET remote_id=?,state='PENDING' WHERE task_id=? AND state='CREATING'", (remote_id, task_id))

    def todoist_finish(self, task_id, remote_completed):
        with self._transaction() as db:
            local = db.execute('SELECT completed_at FROM tasks WHERE id=?', (task_id,)).fetchone()
            if local is None:
                return
            state = 'SYNCED' if bool(local[0] is not None) == remote_completed else 'PENDING'
            db.execute('UPDATE todoist_outbox SET state=?,remote_completed=?,last_error=NULL,retry_at=0,last_checked=? WHERE task_id=?',
                       (state, int(remote_completed), utc_now(), task_id))

    def todoist_fail(self, task_id, code, *, ambiguous=False, retry_after=90):
        state = ('UNCERTAIN' if ambiguous else 'AUTH_REQUIRED' if code == 'AUTH_REQUIRED'
                 else 'PENDING' if code in {'RATE_LIMITED', 'REQUEST_FAILED'} else 'BLOCKED')
        with self._transaction() as db:
            db.execute('UPDATE todoist_outbox SET state=?,last_error=?,retry_at=? WHERE task_id=?',
                       (state, code, utc_now() + max(30, min(3600, retry_after)), task_id))

    def todoist_checks(self, limit=3):
        with self._transaction() as db:
            rows = db.execute("SELECT task_id,remote_id FROM todoist_outbox WHERE state='SYNCED' AND remote_completed=0 AND last_checked<=? ORDER BY last_checked,task_id LIMIT ?",
                              (utc_now() - 90, limit)).fetchall()
            for row in rows:
                db.execute('UPDATE todoist_outbox SET last_checked=? WHERE task_id=?', (utc_now(), row['task_id']))
            return [dict(row) for row in rows]

    def todoist_remote_complete(self, task_id):
        with self._transaction() as db:
            db.execute('UPDATE tasks SET completed_at=? WHERE id=? AND completed_at IS NULL', (utc_now(), task_id))
            db.execute("UPDATE todoist_outbox SET remote_completed=1,state='SYNCED',last_error=NULL WHERE task_id=?", (task_id,))

    def todoist_counts(self):
        if not self._todoist_enabled:
            return {}
        with self._lock:
            rows = self._connection.execute('SELECT state,COUNT(*) FROM todoist_outbox GROUP BY state').fetchall()
            return {str(row[0]): int(row[1]) for row in rows}

    def todoist_task_registered(self, task_id):
        """Only a persisted remote identity proves successful Todoist creation."""
        if not self._todoist_enabled:
            return False
        with self._lock:
            row = self._connection.execute('SELECT remote_id FROM todoist_outbox WHERE task_id=?', (task_id,)).fetchone()
            return bool(row and row[0])

    def pending_reminders(self, *, now: int | float | datetime | None = None) -> list[dict[str, object]]:
        now_ts = utc_timestamp(now) if now is not None else utc_now()
        assert now_ts is not None
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            rows = self._connection.execute(
                "SELECT id, title, due_at, created_at, completed_at, reminder_sent_at "
                "FROM tasks WHERE completed_at IS NULL AND due_at IS NOT NULL "
                "AND due_at <= ? AND reminder_sent_at IS NULL "
                "ORDER BY due_at, created_at, id",
                (now_ts,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def mark_reminder_sent(self, task_id: str, *, sent_at: int | None = None) -> bool:
        task_id = _validate_task_id(task_id)
        sent_at = utc_now() if sent_at is None else utc_timestamp(sent_at)
        assert sent_at is not None
        with self._transaction() as connection:
            result = connection.execute(
                "UPDATE tasks SET reminder_sent_at = ? "
                "WHERE id = ? AND completed_at IS NULL AND reminder_sent_at IS NULL",
                (sent_at, task_id),
            )
        return result.rowcount == 1

    def add_game_credit(self, game: str, credits: int, *, note: str = "") -> dict[str, object]:
        game = _validate_text(game, field_name="game", max_length=80)
        if not isinstance(credits, int) or isinstance(credits, bool):
            raise TypeError("credits must be an integer")
        if credits < 0:
            raise ValueError("credits must be non-negative")
        if credits > 1_000_000_000:
            raise ValueError("credits is too large")
        if not isinstance(note, str):
            raise TypeError("note must be text")
        note = note.strip()
        if len(note) > 500:
            raise ValueError("note is too long")
        created_at = utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO game_credits(game, credits, note, created_at) VALUES (?, ?, ?, ?)",
                (game, credits, note, created_at),
            )
            credit_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT id, game, credits, note, created_at FROM game_credits WHERE id = ?",
                (credit_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_game_credits(self, *, game: str | None = None) -> list[dict[str, object]]:
        params: tuple[object, ...] = ()
        where = ""
        if game is not None:
            game = _validate_text(game, field_name="game", max_length=80)
            where = "WHERE game = ?"
            params = (game,)
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            rows = self._connection.execute(
                "SELECT id, game, credits, note, created_at FROM game_credits "
                f"{where} ORDER BY created_at, id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def find_image_by_hash(self, sha256: str) -> dict[str, object] | None:
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            row = self._connection.execute(
                "SELECT id, original_name, stored_name, stored_path, sha256, size, "
                "content_type, created_at FROM images WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
        return dict(row) if row else None

    def register_image(
        self,
        *,
        image_id: str,
        original_name: str,
        stored_name: str,
        stored_path: str,
        sha256: str,
        size: int,
        content_type: str,
    ) -> dict[str, object]:
        image_id = _validate_task_id(image_id)
        original_name = _validate_text(original_name, field_name="original_name", max_length=255)
        stored_name = _validate_text(stored_name, field_name="stored_name", max_length=255)
        stored_path = _validate_text(stored_path, field_name="stored_path", max_length=4096)
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ValueError("sha256 must be lowercase hexadecimal")
        if size <= 0:
            raise ValueError("image size must be positive")
        content_type = _validate_text(content_type, field_name="content_type", max_length=80)
        created_at = utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id, original_name, stored_name, stored_path, sha256, size, "
                "content_type, created_at FROM images WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            if existing:
                return dict(existing)
            try:
                connection.execute(
                    "INSERT INTO images(id, original_name, stored_name, stored_path, sha256, "
                    "size, content_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        image_id,
                        original_name,
                        stored_name,
                        stored_path,
                        sha256,
                        size,
                        content_type,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError("image metadata conflicts with an existing record") from exc
            row = connection.execute(
                "SELECT id, original_name, stored_name, stored_path, sha256, size, "
                "content_type, created_at FROM images WHERE id = ?",
                (image_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_images(self) -> list[dict[str, object]]:
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            rows = self._connection.execute(
                "SELECT id, original_name, stored_name, stored_path, sha256, size, "
                "content_type, created_at FROM images ORDER BY created_at, id"
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, object]:
        with self._lock:
            if self._connection is None:
                raise StorageError("storage is closed")
            task_count = self._connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            open_task_count = self._connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE completed_at IS NULL"
            ).fetchone()[0]
            credit_count = self._connection.execute("SELECT COUNT(*) FROM game_credits").fetchone()[0]
            image_count = self._connection.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            migration = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        return {
            "database_path": str(self.database_path),
            "schema_version": int(migration),
            "task_count": int(task_count),
            "open_task_count": int(open_task_count),
            "credit_entry_count": int(credit_count),
            "image_count": int(image_count),
            "pending_reminder_count": len(self.pending_reminders()),
        }

    def backup_to(self, destination: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
        """Create an atomic SQLite backup without overwriting by default."""

        destination_path = Path(destination).expanduser()
        if destination_path.resolve() == self.database_path.resolve():
            raise ValueError("backup destination must differ from the live database")
        if destination_path.exists() and not overwrite:
            raise FileExistsError(f"backup already exists: {destination_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination_path.name}.",
                suffix=".tmp",
                dir=destination_path.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            with self._lock:
                if self._connection is None:
                    raise StorageError("storage is closed")
                backup_connection = sqlite3.connect(str(temporary_path))
                try:
                    self._connection.backup(backup_connection)
                finally:
                    backup_connection.close()
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
        return destination_path

    def export_status_json(self, *, settings: Mapping[str, object] | None = None) -> str:
        """Return a compact JSON status payload for CLI and diagnostics."""

        payload: dict[str, object] = {"storage": self.status()}
        if settings is not None:
            payload["config"] = dict(settings)
        payload["integrations"] = {
            "discord": "NOT_CONFIGURED",
            "gmail": "NOT_CONFIGURED",
            "google_tasks": "NOT_CONFIGURED",
            "llm": "NOT_CONFIGURED",
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
