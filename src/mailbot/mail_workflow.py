"""Offline mail ingest, notification, and reaction workflow.

This module is deliberately independent from mailbot.storage.  It keeps the
next mail prototype's schema, migrations, and state transitions in a separate
SQLite database so the existing task and attachment store can remain
unchanged.

The workflow never contacts Gmail, Discord, or an LLM.  .eml input is parsed
by the existing mail_preview.preview helper and the bounded body excerpt is
labelled as an extractive preview.  Discord deliveries and mail actions are
local records only.
"""

from __future__ import annotations

from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Iterator

from .mail_preview import MAX_MESSAGE_BYTES, preview


SCHEMA_VERSION = 1
DEFAULT_EXCERPT_LIMIT = 600
MAX_ID_LENGTH = 512
MAX_NOTIFICATION_LENGTH = 2000
ALLOWED_REACTIONS = {
    "✅": "mark_read",
    "📥": "archive",
    "⭐": "star",
}
VALID_ID_KINDS = frozenset({"synthetic", "external"})
SIMULATED = "SIMULATED"
NOT_CONFIGURED = "NOT_CONFIGURED"


class MailWorkflowError(RuntimeError):
    """Base error for local mail workflow failures."""


class WorkflowStorageError(MailWorkflowError):
    """Raised when the mail workflow database cannot be used safely."""


class AuthorizationError(MailWorkflowError):
    """Raised when an offline Discord target or actor is not allowlisted."""


class StableMessageIdRequired(MailWorkflowError):
    """Raised when an offline message has no stable identity."""


class DuplicateMessageConflict(MailWorkflowError):
    """Raised when the same identity is ingested with different content."""


class UnknownMessage(MailWorkflowError):
    """Raised when a requested message identity does not exist."""


class UnknownDelivery(MailWorkflowError):
    """Raised when a requested fictional Discord delivery does not exist."""


def _now() -> int:
    return int(time.time())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _positive_id(value: object, *, field_name: str) -> int:
    """Normalize a Discord-like numeric ID without accepting booleans."""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    if isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            raise ValueError(f"{field_name} must be a positive integer")
        value = int(text)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _id_set(values: object, *, field_name: str) -> frozenset[int]:
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)):
        values = [values]
    try:
        items = list(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{field_name} must be an iterable of IDs") from exc
    return frozenset(_positive_id(item, field_name=field_name) for item in items)


def _text(
    value: object,
    *,
    field_name: str,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _message_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("message_id must be text")
    normalized = value.strip()
    if not normalized:
        raise StableMessageIdRequired("a stable message_id is required")
    if len(normalized) > MAX_ID_LENGTH:
        raise ValueError("message_id is too long")
    if any(character.isspace() or ord(character) < 32 for character in normalized):
        raise ValueError("message_id must not contain whitespace or control characters")
    return normalized


def _id_kind(value: object) -> str:
    if not isinstance(value, str) or value not in VALID_ID_KINDS:
        raise ValueError("message_id_kind must be 'synthetic' or 'external'")
    return value


def _identity_key(message_id: str, message_id_kind: str) -> str:
    # The prefix makes an offline synthetic identity visibly distinct from a
    # provider identifier.  It also prevents an accidental future Gmail ID
    # from being mistaken for a locally generated value.
    return f"{message_id_kind}:{message_id}"


def _private_parent(path: Path) -> None:
    """Create database parent directories with private permissions.

    Only directories within the requested database path are touched.  Common
    shared roots and the current working directory are left alone.
    """

    resolved_parent = path.expanduser().resolve().parent
    missing: list[Path] = []
    current = resolved_parent
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if current.exists() and not current.is_dir():
        raise WorkflowStorageError(f"database parent is not a directory: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return dict(row) if row is not None else None


class MailWorkflowStore:
    """Durable offline mail workflow state.

    settings is optional so local simulation does not require a Discord token.
    When explicit allowlists are passed, they are used directly.  When no
    explicit allowlists are passed, the three ID sets are copied from
    Settings.  Empty sets always deny target and reaction operations.
    """

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        settings: object | None = None,
        *,
        allowed_guild_ids: object | None = None,
        allowed_channel_ids: object | None = None,
        allowed_user_ids: object | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self._is_memory = str(database_path) == ":memory:"
        if not self._is_memory:
            if self.database_path.exists() and self.database_path.is_symlink():
                raise WorkflowStorageError("mail workflow database must not be a symlink")
            _private_parent(self.database_path)
        self._settings = settings
        if (
            allowed_guild_ids is None
            and allowed_channel_ids is None
            and allowed_user_ids is None
            and settings is not None
        ):
            allowed_guild_ids = getattr(settings, "allowed_guild_ids", None)
            allowed_channel_ids = getattr(settings, "allowed_channel_ids", None)
            allowed_user_ids = getattr(settings, "allowed_user_ids", None)
        self.allowed_guild_ids = _id_set(
            allowed_guild_ids,
            field_name="allowed_guild_ids",
        )
        self.allowed_channel_ids = _id_set(
            allowed_channel_ids,
            field_name="allowed_channel_ids",
        )
        self.allowed_user_ids = _id_set(
            allowed_user_ids,
            field_name="allowed_user_ids",
        )
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            str(self.database_path),
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._connection.execute("PRAGMA busy_timeout = 10000")
                self._connection.execute("PRAGMA foreign_keys = ON")
                if not self._is_memory:
                    self._connection.execute("PRAGMA journal_mode = WAL")
                    self._connection.execute("PRAGMA synchronous = NORMAL")
                self._migrate()
                self._enforce_file_mode()
        except Exception:
            self._connection.close()
            self._connection = None
            raise

    def __enter__(self) -> "MailWorkflowStore":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _enforce_file_mode(self) -> None:
        if not self._is_memory:
            os.chmod(self.database_path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.database_path}{suffix}")
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connection
            if connection is None:
                raise WorkflowStorageError("mail workflow storage is closed")
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
                self._enforce_file_mode()
            except Exception:
                connection.rollback()
                raise

    def _migrate(self) -> None:
        """Apply migrations atomically and reject a database from the future."""

        connection = self._connection
        assert connection is not None
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mail_workflow_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM mail_workflow_schema_migrations"
            ).fetchone()
            current = int(row["version"])
            if current > SCHEMA_VERSION:
                raise WorkflowStorageError(
                    "mail workflow database schema is newer than this prototype"
                )
            if current < 1:
                ddl = (
                    """
                    CREATE TABLE mail_messages (
                        identity_key TEXT PRIMARY KEY,
                        message_id TEXT NOT NULL,
                        message_id_kind TEXT NOT NULL
                            CHECK (message_id_kind IN ('synthetic', 'external')),
                        source_hash TEXT NOT NULL
                            CHECK (length(source_hash) = 64),
                        identity_hash TEXT NOT NULL
                            CHECK (length(identity_hash) = 64),
                        sender TEXT NOT NULL CHECK (length(sender) BETWEEN 1 AND 300),
                        subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 300),
                        body_excerpt TEXT NOT NULL CHECK (length(body_excerpt) BETWEEN 1 AND 600),
                        preview_mode TEXT NOT NULL,
                        llm_status TEXT NOT NULL,
                        source_kind TEXT NOT NULL
                            CHECK (source_kind = 'offline_eml'),
                        created_at INTEGER NOT NULL,
                        UNIQUE (message_id_kind, message_id)
                    )
                    """,
                    """
                    CREATE TABLE notification_outbox (
                        notification_key TEXT PRIMARY KEY,
                        identity_key TEXT NOT NULL UNIQUE
                            REFERENCES mail_messages(identity_key) ON DELETE CASCADE,
                        rendered_text TEXT NOT NULL
                            CHECK (length(rendered_text) BETWEEN 1 AND 2000),
                        status TEXT NOT NULL
                            CHECK (status IN ('PENDING', 'DELIVERED')),
                        created_at INTEGER NOT NULL,
                        delivered_at INTEGER
                    )
                    """,
                    """
                    CREATE TABLE discord_deliveries (
                        delivery_key TEXT PRIMARY KEY,
                        notification_key TEXT NOT NULL
                            REFERENCES notification_outbox(notification_key)
                            ON DELETE CASCADE,
                        guild_id INTEGER NOT NULL CHECK (guild_id > 0),
                        channel_id INTEGER NOT NULL CHECK (channel_id > 0),
                        delivery_id TEXT NOT NULL UNIQUE,
                        delivery_mode TEXT NOT NULL CHECK (delivery_mode = 'SIMULATED'),
                        created_at INTEGER NOT NULL,
                        UNIQUE (notification_key, guild_id, channel_id)
                    )
                    """,
                    """
                    CREATE TABLE reaction_states (
                        delivery_key TEXT NOT NULL
                            REFERENCES discord_deliveries(delivery_key)
                            ON DELETE CASCADE,
                        emoji TEXT NOT NULL
                            CHECK (emoji IN ('✅', '📥', '⭐')),
                        user_id INTEGER NOT NULL CHECK (user_id > 0),
                        guild_id INTEGER NOT NULL CHECK (guild_id > 0),
                        channel_id INTEGER NOT NULL CHECK (channel_id > 0),
                        active INTEGER NOT NULL CHECK (active IN (0, 1)),
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (delivery_key, emoji, user_id)
                    )
                    """,
                    """
                    CREATE TABLE reaction_events (
                        event_key TEXT PRIMARY KEY,
                        delivery_key TEXT NOT NULL
                            REFERENCES discord_deliveries(delivery_key)
                            ON DELETE CASCADE,
                        emoji TEXT NOT NULL
                            CHECK (emoji IN ('✅', '📥', '⭐')),
                        user_id INTEGER NOT NULL CHECK (user_id > 0),
                        operation TEXT NOT NULL CHECK (operation IN ('add', 'remove')),
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        created_at INTEGER NOT NULL
                    )
                    """,
                    """
                    CREATE TABLE mail_actions (
                        action_key TEXT PRIMARY KEY,
                        identity_key TEXT NOT NULL
                            REFERENCES mail_messages(identity_key)
                            ON DELETE CASCADE,
                        delivery_key TEXT NOT NULL
                            REFERENCES discord_deliveries(delivery_key)
                            ON DELETE CASCADE,
                        emoji TEXT NOT NULL
                            CHECK (emoji IN ('✅', '📥', '⭐')),
                        action_kind TEXT NOT NULL
                            CHECK (action_kind IN ('mark_read', 'archive', 'star')),
                        user_id INTEGER NOT NULL CHECK (user_id > 0),
                        guild_id INTEGER NOT NULL CHECK (guild_id > 0),
                        channel_id INTEGER NOT NULL CHECK (channel_id > 0),
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        status TEXT NOT NULL CHECK (status IN ('PENDING', 'CANCELLED')),
                        provider_status TEXT NOT NULL
                            CHECK (provider_status = 'NOT_CONFIGURED'),
                        created_at INTEGER NOT NULL,
                        cancelled_at INTEGER,
                        UNIQUE (delivery_key, emoji, user_id, generation)
                    )
                    """,
                    "CREATE INDEX mail_messages_created_idx "
                    "ON mail_messages(created_at, identity_key)",
                    "CREATE INDEX notification_outbox_status_idx "
                    "ON notification_outbox(status, created_at)",
                    "CREATE INDEX mail_actions_status_idx "
                    "ON mail_actions(status, created_at)",
                    "INSERT INTO mail_workflow_schema_migrations(version, applied_at) "
                    "VALUES (1, ?)",
                )
                for statement in ddl[:-1]:
                    connection.execute(statement)
                connection.execute(ddl[-1], (_now(),))
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise WorkflowStorageError("mail workflow storage is closed")
        return self._connection

    def _authorize_target(
        self,
        *,
        guild_id: object,
        channel_id: object,
        user_id: object | None = None,
        require_user: bool = False,
    ) -> tuple[int, int, int | None]:
        guild = _positive_id(guild_id, field_name="guild_id")
        channel = _positive_id(channel_id, field_name="channel_id")
        user = (
            _positive_id(user_id, field_name="user_id")
            if user_id is not None
            else None
        )
        if (
            not self.allowed_guild_ids
            or not self.allowed_channel_ids
            or guild not in self.allowed_guild_ids
            or channel not in self.allowed_channel_ids
        ):
            raise AuthorizationError("offline guild/channel target is not allowlisted")
        if require_user and (
            not self.allowed_user_ids
            or user is None
            or user not in self.allowed_user_ids
        ):
            raise AuthorizationError("offline reaction user is not allowlisted")
        if user is not None and (
            not self.allowed_user_ids or user not in self.allowed_user_ids
        ):
            raise AuthorizationError("offline user is not allowlisted")
        return guild, channel, user

    @staticmethod
    def _read_eml(path: str | os.PathLike[str], *, max_bytes: int) -> bytes:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if max_bytes > MAX_MESSAGE_BYTES:
            raise ValueError(f"max_bytes must not exceed {MAX_MESSAGE_BYTES}")
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"offline .eml file does not exist: {source}")
        with source.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"offline .eml exceeds the {max_bytes}-byte limit")
        return raw

    @staticmethod
    def _header_message_id(raw: bytes) -> str | None:
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        header = message.get("Message-ID")
        if header is None:
            return None
        text = str(header).strip()
        return text or None

    @staticmethod
    def _canonical_identity(
        *,
        message_id: str,
        message_id_kind: str,
        sender: str,
        subject: str,
        body_excerpt: str,
    ) -> str:
        canonical = json.dumps(
            {
                "body_excerpt": body_excerpt,
                "message_id": message_id,
                "message_id_kind": message_id_kind,
                "sender": sender,
                "subject": subject,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _sha256(canonical)

    @staticmethod
    def _notification_text(
        *,
        identity_key: str,
        sender: str,
        subject: str,
        body_excerpt: str,
    ) -> str:
        text = (
            "✉️ Offline mail\n"
            f"ID: {identity_key}\n"
            f"From: {sender}\n"
            f"Subject: {subject}\n"
            f"Excerpt (OFFLINE_EXTRACTIVE_PREVIEW; LLM=NOT_CONFIGURED): "
            f"{body_excerpt}"
        )
        if len(text) > MAX_NOTIFICATION_LENGTH:
            # The individual fields are already bounded.  This guard protects
            # the Discord-facing render contract if the limits change later.
            text = text[:MAX_NOTIFICATION_LENGTH]
        return text

    @staticmethod
    def _message_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "identity_key": row["identity_key"],
            "message_id": row["message_id"],
            "message_id_kind": row["message_id_kind"],
            "source_hash": row["source_hash"],
            "identity_hash": row["identity_hash"],
            "sender": row["sender"],
            "subject": row["subject"],
            "body_excerpt": row["body_excerpt"],
            "preview_mode": row["preview_mode"],
            "llm_status": row["llm_status"],
            "source_kind": row["source_kind"],
            "created_at": row["created_at"],
        }

    def ingest_eml(
        self,
        path: str | os.PathLike[str] | bytes,
        *,
        message_id: str | None = None,
        source_id: str | None = None,
        message_id_kind: str = "synthetic",
        max_bytes: int = MAX_MESSAGE_BYTES,
        excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
    ) -> dict[str, object]:
        """Ingest one bounded local .eml and enqueue one notification.

        A caller-supplied ID is preferred for synthetic fixtures.  If omitted,
        an RFC Message-ID header is accepted as a synthetic offline ID.  No
        random or Gmail-shaped ID is generated.
        """

        message_id_kind = _id_kind(message_id_kind)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if max_bytes > MAX_MESSAGE_BYTES:
            raise ValueError(f"max_bytes must not exceed {MAX_MESSAGE_BYTES}")
        if source_id is not None:
            if message_id is not None and source_id != message_id:
                raise ValueError("message_id and source_id must match when both are provided")
            message_id = source_id
        if isinstance(path, bytes):
            if len(path) > max_bytes:
                raise ValueError(f"offline .eml exceeds the {max_bytes}-byte limit")
            raw = path
        else:
            raw = self._read_eml(path, max_bytes=max_bytes)
        if message_id is None:
            message_id = self._header_message_id(raw)
            if message_id is None:
                raise StableMessageIdRequired(
                    "a stable message_id or Message-ID header is required"
                )
        else:
            message_id = _message_id(message_id)
        message_id = _message_id(message_id)
        if not isinstance(excerpt_limit, int) or isinstance(excerpt_limit, bool):
            raise ValueError("excerpt_limit must be an integer")
        if not 1 <= excerpt_limit <= 600:
            raise ValueError("excerpt_limit must be between 1 and 600")
        parsed = preview(raw, limit=excerpt_limit)
        sender = _text(
            parsed.get("sender", ""),
            field_name="sender",
            max_length=300,
            allow_empty=True,
        ) or "(unknown sender)"
        subject = _text(
            parsed.get("subject", ""),
            field_name="subject",
            max_length=300,
            allow_empty=True,
        ) or "(no subject)"
        body_excerpt = _text(
            parsed.get("preview", ""),
            field_name="body_excerpt",
            max_length=600,
            allow_empty=True,
        ) or "(no plain text body)"
        identity_key = _identity_key(message_id, message_id_kind)
        source_hash = _sha256(raw)
        identity_hash = self._canonical_identity(
            message_id=message_id,
            message_id_kind=message_id_kind,
            sender=sender,
            subject=subject,
            body_excerpt=body_excerpt,
        )
        notification_key = f"notification:{identity_key}"
        rendered_text = self._notification_text(
            identity_key=identity_key,
            sender=sender,
            subject=subject,
            body_excerpt=body_excerpt,
        )
        created_at = _now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM mail_messages WHERE identity_key = ?",
                (identity_key,),
            ).fetchone()
            if existing is not None:
                if existing["source_hash"] != source_hash:
                    raise DuplicateMessageConflict(
                        "message identity already exists with different content"
                    )
                result = self._message_row(existing)
                result.update(
                    {
                        "notification_key": notification_key,
                        "deduplicated": True,
                        "created": False,
                    }
                )
                return result
            connection.execute(
                """
                INSERT INTO mail_messages(
                    identity_key, message_id, message_id_kind, source_hash,
                    identity_hash, sender, subject, body_excerpt, preview_mode,
                    llm_status, source_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity_key,
                    message_id,
                    message_id_kind,
                    source_hash,
                    identity_hash,
                    sender,
                    subject,
                    body_excerpt,
                    str(parsed["mode"]),
                    str(parsed["llm"]),
                    "offline_eml",
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO notification_outbox(
                    notification_key, identity_key, rendered_text, status, created_at
                ) VALUES (?, ?, ?, 'PENDING', ?)
                """,
                (notification_key, identity_key, rendered_text, created_at),
            )
        return {
            "identity_key": identity_key,
            "message_id": message_id,
            "message_id_kind": message_id_kind,
            "source_hash": source_hash,
            "identity_hash": identity_hash,
            "sender": sender,
            "subject": subject,
            "body_excerpt": body_excerpt,
            "preview_mode": str(parsed["mode"]),
            "llm_status": str(parsed["llm"]),
            "source_kind": "offline_eml",
            "notification_key": notification_key,
            "deduplicated": False,
            "created": True,
        }

    def _resolve_message(
        self,
        message_id: str,
        message_id_kind: str | None = None,
    ) -> sqlite3.Row:
        if not isinstance(message_id, str):
            raise TypeError("message_id must be text")
        candidate = message_id.strip()
        if not candidate:
            raise UnknownMessage("message identity is empty")
        if message_id_kind is not None:
            key = _identity_key(_message_id(candidate), _id_kind(message_id_kind))
            keys = (key,)
        elif candidate.startswith("synthetic:") or candidate.startswith("external:"):
            keys = (candidate,)
        else:
            keys = (_identity_key(_message_id(candidate), "synthetic"),)
        with self._lock:
            connection = self._require_open()
            row = connection.execute(
                "SELECT * FROM mail_messages WHERE identity_key IN ({})".format(
                    ",".join("?" for _ in keys)
                ),
                keys,
            ).fetchone()
        if row is None:
            raise UnknownMessage(f"unknown offline message: {candidate}")
        return row

    def get_message(
        self,
        message_id: str,
        message_id_kind: str | None = None,
    ) -> dict[str, object]:
        return self._message_row(self._resolve_message(message_id, message_id_kind))

    def list_messages(self) -> list[dict[str, object]]:
        with self._lock:
            connection = self._require_open()
            rows = connection.execute(
                "SELECT * FROM mail_messages ORDER BY created_at, identity_key"
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def render_notification(
        self,
        message_id: str,
        message_id_kind: str | None = None,
    ) -> dict[str, object]:
        row = self._resolve_message(message_id, message_id_kind)
        with self._lock:
            connection = self._require_open()
            notification = connection.execute(
                "SELECT * FROM notification_outbox WHERE identity_key = ?",
                (row["identity_key"],),
            ).fetchone()
        if notification is None:
            raise WorkflowStorageError("message notification outbox row is missing")
        return {
            "notification_key": notification["notification_key"],
            "identity_key": notification["identity_key"],
            "rendered_text": notification["rendered_text"],
            "status": notification["status"],
            "delivery_mode": SIMULATED,
            "provider_status": NOT_CONFIGURED,
        }

    def list_notifications(self) -> list[dict[str, object]]:
        with self._lock:
            connection = self._require_open()
            rows = connection.execute(
                """
                SELECT notification_key, identity_key, status, created_at, delivered_at
                FROM notification_outbox
                ORDER BY created_at, notification_key
                """
            ).fetchall()
        return [
            {
                "notification_key": row["notification_key"],
                "identity_key": row["identity_key"],
                "status": row["status"],
                "created_at": row["created_at"],
                "delivered_at": row["delivered_at"],
                "delivery_mode": SIMULATED,
                "provider_status": NOT_CONFIGURED,
            }
            for row in rows
        ]

    @staticmethod
    def _delivery_ids(
        notification_key: str,
        guild_id: int,
        channel_id: int,
    ) -> tuple[str, str]:
        seed = f"{notification_key}|guild={guild_id}|channel={channel_id}".encode("utf-8")
        digest = _sha256(seed)[:32]
        return f"simulated:{digest}", f"simulated-discord:{digest}"

    def simulate_delivery(
        self,
        message_id: str,
        guild_id: object,
        channel_id: object,
        *,
        user_id: object | None = None,
        message_id_kind: str | None = None,
    ) -> dict[str, object]:
        """Create a deterministic fictional Discord mapping.

        user_id is optional for a local sender-less simulation.  If supplied,
        it must be allowlisted.  Guild and channel are always required and
        default-deny.
        """

        guild, channel, actor = self._authorize_target(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            require_user=False,
        )
        row = self._resolve_message(message_id, message_id_kind)
        notification = self.render_notification(str(row["identity_key"]))
        delivery_key, delivery_id = self._delivery_ids(
            str(notification["notification_key"]),
            guild,
            channel,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM discord_deliveries WHERE delivery_key = ?",
                (delivery_key,),
            ).fetchone()
            if existing is not None:
                result = dict(existing)
                result.update(
                    {
                        "deduplicated": True,
                        "notification_status": "DELIVERED",
                        "provider_status": NOT_CONFIGURED,
                        "actor_user_id": actor,
                    }
                )
                return result
            now = _now()
            connection.execute(
                """
                INSERT INTO discord_deliveries(
                    delivery_key, notification_key, guild_id, channel_id,
                    delivery_id, delivery_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, 'SIMULATED', ?)
                """,
                (
                    delivery_key,
                    notification["notification_key"],
                    guild,
                    channel,
                    delivery_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'DELIVERED', delivered_at = COALESCE(delivered_at, ?)
                WHERE notification_key = ?
                """,
                (now, notification["notification_key"]),
            )
            row = connection.execute(
                "SELECT * FROM discord_deliveries WHERE delivery_key = ?",
                (delivery_key,),
            ).fetchone()
        assert row is not None
        result = dict(row)
        result.update(
            {
                "deduplicated": False,
                "notification_status": "DELIVERED",
                "provider_status": NOT_CONFIGURED,
                "actor_user_id": actor,
            }
        )
        return result

    def list_deliveries(self) -> list[dict[str, object]]:
        with self._lock:
            connection = self._require_open()
            rows = connection.execute(
                "SELECT * FROM discord_deliveries ORDER BY created_at, delivery_key"
            ).fetchall()
        return [
            {
                **dict(row),
                "provider_status": NOT_CONFIGURED,
            }
            for row in rows
        ]

    def _resolve_delivery(self, delivery_id: str) -> sqlite3.Row:
        if not isinstance(delivery_id, str) or not delivery_id.strip():
            raise UnknownDelivery("delivery_id is required")
        candidate = delivery_id.strip()
        with self._lock:
            connection = self._require_open()
            row = connection.execute(
                "SELECT * FROM discord_deliveries "
                "WHERE delivery_key = ? OR delivery_id = ?",
                (candidate, candidate),
            ).fetchone()
        if row is None:
            raise UnknownDelivery(f"unknown simulated delivery: {candidate}")
        return row

    @staticmethod
    def _action_row(
        connection: sqlite3.Connection,
        action_key: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT * FROM mail_actions WHERE action_key = ?",
            (action_key,),
        ).fetchone()
        return _row_to_dict(row)

    def react(
        self,
        delivery_id: str,
        emoji: str,
        *,
        user_id: object,
        guild_id: object,
        channel_id: object,
        added: bool = True,
        operation: str | None = None,
    ) -> dict[str, object]:
        """Record a reaction intent without executing a mail provider call.

        Adding an active reaction again is idempotent.  Removing an active
        reaction cancels its still-pending action, and adding it again creates
        a new generation and therefore a new pending action record.
        """

        if operation is not None:
            if operation not in {"add", "remove"}:
                raise ValueError("operation must be 'add' or 'remove'")
            added = operation == "add"
        if not isinstance(added, bool):
            raise TypeError("added must be a boolean")
        if emoji not in ALLOWED_REACTIONS:
            raise ValueError("reaction emoji is not allowed")
        guild, channel, user = self._authorize_target(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            require_user=True,
        )
        assert user is not None
        delivery = self._resolve_delivery(delivery_id)
        if int(delivery["guild_id"]) != guild or int(delivery["channel_id"]) != channel:
            raise AuthorizationError("reaction target guild/channel does not match delivery")
        delivery_key = str(delivery["delivery_key"])
        with self._transaction() as connection:
            state = connection.execute(
                """
                SELECT * FROM reaction_states
                WHERE delivery_key = ? AND emoji = ? AND user_id = ?
                """,
                (delivery_key, emoji, user),
            ).fetchone()
            now = _now()
            if added:
                if state is not None and int(state["active"]) == 1:
                    action = connection.execute(
                        """
                        SELECT * FROM mail_actions
                        WHERE delivery_key = ? AND emoji = ? AND user_id = ?
                          AND generation = ?
                        """,
                        (delivery_key, emoji, user, state["generation"]),
                    ).fetchone()
                    return {
                        "status": "DUPLICATE",
                        "reaction": dict(state),
                        "action": _row_to_dict(action),
                        "delivery": dict(delivery),
                    }
                generation = 1 if state is None else int(state["generation"]) + 1
                if state is None:
                    connection.execute(
                        """
                        INSERT INTO reaction_states(
                            delivery_key, emoji, user_id, guild_id, channel_id,
                            active, generation, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (delivery_key, emoji, user, guild, channel, generation, now),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE reaction_states
                        SET guild_id = ?, channel_id = ?, active = 1,
                            generation = ?, updated_at = ?
                        WHERE delivery_key = ? AND emoji = ? AND user_id = ?
                        """,
                        (
                            guild,
                            channel,
                            generation,
                            now,
                            delivery_key,
                            emoji,
                            user,
                        ),
                    )
                event_key = _sha256(
                    f"{delivery_key}|{emoji}|{user}|add|{generation}".encode("utf-8")
                )
                connection.execute(
                    """
                    INSERT INTO reaction_events(
                        event_key, delivery_key, emoji, user_id, operation,
                        generation, created_at
                    ) VALUES (?, ?, ?, ?, 'add', ?, ?)
                    """,
                    (event_key, delivery_key, emoji, user, generation, now),
                )
                action_key = f"action:{delivery_key}:{emoji}:{user}:{generation}"
                connection.execute(
                    """
                    INSERT INTO mail_actions(
                        action_key, identity_key, delivery_key, emoji, action_kind,
                        user_id, guild_id, channel_id, generation, status,
                        provider_status, created_at
                    )
                    SELECT ?, m.identity_key, ?, ?, ?, ?, ?, ?, ?, 'PENDING',
                           'NOT_CONFIGURED', ?
                    FROM notification_outbox n
                    JOIN mail_messages m ON m.identity_key = n.identity_key
                    JOIN discord_deliveries d ON d.notification_key = n.notification_key
                    WHERE d.delivery_key = ?
                    """,
                    (
                        action_key,
                        delivery_key,
                        emoji,
                        ALLOWED_REACTIONS[emoji],
                        user,
                        guild,
                        channel,
                        generation,
                        now,
                        delivery_key,
                    ),
                )
                action = self._action_row(connection, action_key)
                return {
                    "status": "PENDING",
                    "reaction": {
                        "delivery_key": delivery_key,
                        "emoji": emoji,
                        "user_id": user,
                        "guild_id": guild,
                        "channel_id": channel,
                        "active": 1,
                        "generation": generation,
                        "updated_at": now,
                    },
                    "action": action,
                    "delivery": dict(delivery),
                }

            if state is None or int(state["active"]) == 0:
                return {
                    "status": "DUPLICATE",
                    "reaction": _row_to_dict(state),
                    "action": None,
                    "delivery": dict(delivery),
                }
            generation = int(state["generation"])
            connection.execute(
                """
                UPDATE reaction_states SET active = 0, updated_at = ?
                WHERE delivery_key = ? AND emoji = ? AND user_id = ?
                """,
                (now, delivery_key, emoji, user),
            )
            event_key = _sha256(
                f"{delivery_key}|{emoji}|{user}|remove|{generation}".encode("utf-8")
            )
            connection.execute(
                """
                INSERT INTO reaction_events(
                    event_key, delivery_key, emoji, user_id, operation,
                    generation, created_at
                ) VALUES (?, ?, ?, ?, 'remove', ?, ?)
                """,
                (event_key, delivery_key, emoji, user, generation, now),
            )
            connection.execute(
                """
                UPDATE mail_actions
                SET status = 'CANCELLED', cancelled_at = ?
                WHERE delivery_key = ? AND emoji = ? AND user_id = ?
                  AND generation = ? AND status = 'PENDING'
                """,
                (now, delivery_key, emoji, user, generation),
            )
            action = connection.execute(
                """
                SELECT * FROM mail_actions
                WHERE delivery_key = ? AND emoji = ? AND user_id = ?
                  AND generation = ?
                """,
                (delivery_key, emoji, user, generation),
            ).fetchone()
            return {
                "status": "REMOVED",
                "reaction": {
                    "delivery_key": delivery_key,
                    "emoji": emoji,
                    "user_id": user,
                    "guild_id": guild,
                    "channel_id": channel,
                    "active": 0,
                    "generation": generation,
                    "updated_at": now,
                },
                "action": _row_to_dict(action),
                "delivery": dict(delivery),
            }

    def list_actions(self, *, status: str | None = None) -> list[dict[str, object]]:
        if status is not None and status not in {"PENDING", "CANCELLED"}:
            raise ValueError("status must be PENDING or CANCELLED")
        with self._lock:
            connection = self._require_open()
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM mail_actions ORDER BY created_at, action_key"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM mail_actions WHERE status = ? "
                    "ORDER BY created_at, action_key",
                    (status,),
                ).fetchall()
        return [
            {
                **dict(row),
                "delivery_mode": SIMULATED,
            }
            for row in rows
        ]

    def status(self) -> dict[str, object]:
        with self._lock:
            connection = self._require_open()
            counts = {
                "message_count": connection.execute(
                    "SELECT COUNT(*) FROM mail_messages"
                ).fetchone()[0],
                "notification_pending_count": connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE status = 'PENDING'"
                ).fetchone()[0],
                "notification_delivered_count": connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE status = 'DELIVERED'"
                ).fetchone()[0],
                "simulated_delivery_count": connection.execute(
                    "SELECT COUNT(*) FROM discord_deliveries"
                ).fetchone()[0],
                "pending_action_count": connection.execute(
                    "SELECT COUNT(*) FROM mail_actions WHERE status = 'PENDING'"
                ).fetchone()[0],
                "cancelled_action_count": connection.execute(
                    "SELECT COUNT(*) FROM mail_actions WHERE status = 'CANCELLED'"
                ).fetchone()[0],
                "schema_version": connection.execute(
                    "SELECT COALESCE(MAX(version), 0) "
                    "FROM mail_workflow_schema_migrations"
                ).fetchone()[0],
            }
        return {
            "database_path": str(self.database_path),
            **{key: int(value) for key, value in counts.items()},
            "integrations": {
                "gmail": NOT_CONFIGURED,
                "discord": NOT_CONFIGURED,
                "llm": NOT_CONFIGURED,
            },
            "simulation": {
                "delivery_mode": SIMULATED,
                "action_execution": NOT_CONFIGURED,
                "default_deny": True,
            },
            "allowlist": {
                "guild_count": len(self.allowed_guild_ids),
                "channel_count": len(self.allowed_channel_ids),
                "user_count": len(self.allowed_user_ids),
            },
        }


__all__ = [
    "ALLOWED_REACTIONS",
    "AuthorizationError",
    "DEFAULT_EXCERPT_LIMIT",
    "DuplicateMessageConflict",
    "MailWorkflowError",
    "MailWorkflowStore",
    "MAX_MESSAGE_BYTES",
    "NOT_CONFIGURED",
    "SCHEMA_VERSION",
    "SIMULATED",
    "StableMessageIdRequired",
    "UnknownDelivery",
    "UnknownMessage",
    "WorkflowStorageError",
]
