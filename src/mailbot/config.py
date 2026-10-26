"""Configuration parsing and the default-deny Discord authorization guard."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when an environment setting cannot be parsed safely."""


def _path_from_env(value: str) -> Path:
    """Return a user supplied path without touching the filesystem."""

    return Path(value).expanduser()


def parse_discord_id_list(value: str | None, *, setting_name: str) -> frozenset[int]:
    """Parse a comma separated Discord snowflake list.

    Empty lists are intentional: they leave the bot locked down.  Values are
    validated as decimal integers so a typo cannot silently broaden access.
    """

    if not value or not value.strip():
        return frozenset()
    result: set[int] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            raise ConfigurationError(f"{setting_name} contains an empty ID")
        if not item.isdecimal():
            raise ConfigurationError(f"{setting_name} must contain decimal IDs")
        parsed = int(item)
        if parsed <= 0:
            raise ConfigurationError(f"{setting_name} IDs must be positive")
        result.add(parsed)
    return frozenset(result)


def _positive_int(value: str | None, *, setting_name: str, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{setting_name} must be an integer") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{setting_name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings.

    The Discord token is excluded from ``repr`` and all public status output.
    Empty allowlists are a hard deny, including when a token is present.
    """

    database_path: Path = Path("data/mailbot.sqlite3")
    image_dir: Path = Path("data/images")
    max_image_bytes: int = 8 * 1024 * 1024
    discord_token: str | None = field(default=None, repr=False)
    allowed_guild_ids: frozenset[int] = frozenset()
    allowed_channel_ids: frozenset[int] = frozenset()
    allowed_user_ids: frozenset[int] = frozenset()
    gemini_enabled: bool = False
    gemini_preview_consent: bool = False
    gemini_key_path: Path | None = field(default=None, repr=False)
    gemini_model: str = 'gemini-3.5-flash-lite'
    gemini_daily_limit: int = 20
    gmail_actions_enabled: bool = False
    gmail_tasks_enabled: bool = False
    gmail_enabled: bool = False
    gmail_token_path: Path | None = None
    gmail_database_path: Path | None = None
    gmail_poll_seconds: int = 90
    gmail_accounts_path: Path | None = field(default=None, repr=False)
    gmail_trash_enabled: bool = False
    gmail_task_archive_enabled: bool = False
    todoist_enabled: bool = False
    todoist_token_path: Path | None = field(default=None, repr=False)
    todoist_project_id: str | None = None
    todoist_poll_seconds: int = 90

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from ``MAILBOT_*`` and ``DISCORD_*`` variables."""

        env = os.environ if environ is None else environ
        token = env.get("DISCORD_BOT_TOKEN") or None
        gmail_flag = env.get("MAILBOT_GMAIL_ENABLED", "false").strip().lower()
        if gmail_flag not in {"true", "false", "1", "0"}:
            raise ConfigurationError("MAILBOT_GMAIL_ENABLED must be true or false")
        actions_flag = env.get("MAILBOT_GMAIL_ACTIONS_ENABLED", "false").strip().lower()
        if actions_flag not in {"true", "false", "1", "0"}:
            raise ConfigurationError("MAILBOT_GMAIL_ACTIONS_ENABLED must be true or false")
        tasks_flag = env.get('MAILBOT_GMAIL_TASKS_ENABLED', 'false').strip().lower()
        if tasks_flag not in {'true', 'false', '1', '0'}:
            raise ConfigurationError('MAILBOT_GMAIL_TASKS_ENABLED must be true or false')
        gmail_interval = _positive_int(env.get("MAILBOT_GMAIL_POLL_SECONDS"), setting_name="MAILBOT_GMAIL_POLL_SECONDS", default=90)
        if not 30 <= gmail_interval <= 3600:
            raise ConfigurationError("MAILBOT_GMAIL_POLL_SECONDS must be between 30 and 3600")
        gemini_flags = {}
        for name in ('MAILBOT_GEMINI_ENABLED', 'MAILBOT_GEMINI_PREVIEW_CONSENT'):
            value = env.get(name, 'false').strip().lower()
            if value not in {'true', 'false', '1', '0'}:
                raise ConfigurationError(f'{name} must be true or false')
            gemini_flags[name] = value in {'true', '1'}
        gemini_limit = _positive_int(env.get('MAILBOT_GEMINI_DAILY_LIMIT'), setting_name='MAILBOT_GEMINI_DAILY_LIMIT', default=20)
        if gemini_limit > 100:
            raise ConfigurationError('MAILBOT_GEMINI_DAILY_LIMIT must be at most 100')
        todoist_flag = env.get('MAILBOT_TODOIST_ENABLED', 'false').strip().lower()
        if todoist_flag not in {'true', 'false', '1', '0'}:
            raise ConfigurationError('MAILBOT_TODOIST_ENABLED must be true or false')
        todoist_interval = _positive_int(env.get('MAILBOT_TODOIST_POLL_SECONDS'), setting_name='MAILBOT_TODOIST_POLL_SECONDS', default=90)
        if not 30 <= todoist_interval <= 3600:
            raise ConfigurationError('MAILBOT_TODOIST_POLL_SECONDS must be between 30 and 3600')
        gmail_optional = {}
        for name in ('MAILBOT_GMAIL_TRASH_ENABLED', 'MAILBOT_GMAIL_TASK_ARCHIVE_ENABLED'):
            value = env.get(name, 'false').strip().lower()
            if value not in {'true', 'false', '1', '0'}:
                raise ConfigurationError(f'{name} must be true or false')
            gmail_optional[name] = value in {'true', '1'}
        return cls(
            gmail_accounts_path=_path_from_env(env['MAILBOT_GMAIL_ACCOUNTS_PATH']) if env.get('MAILBOT_GMAIL_ACCOUNTS_PATH') else None,
            gmail_trash_enabled=gmail_optional['MAILBOT_GMAIL_TRASH_ENABLED'],
            gmail_task_archive_enabled=gmail_optional['MAILBOT_GMAIL_TASK_ARCHIVE_ENABLED'],
            todoist_enabled=todoist_flag in {'true', '1'},
            todoist_token_path=_path_from_env(env['MAILBOT_TODOIST_TOKEN_PATH']) if env.get('MAILBOT_TODOIST_TOKEN_PATH') else None,
            todoist_project_id=env.get('MAILBOT_TODOIST_PROJECT_ID') or None,
            todoist_poll_seconds=todoist_interval,
            gemini_enabled=gemini_flags['MAILBOT_GEMINI_ENABLED'],
            gemini_preview_consent=gemini_flags['MAILBOT_GEMINI_PREVIEW_CONSENT'],
            gemini_key_path=_path_from_env(env['MAILBOT_GEMINI_KEY_PATH']) if env.get('MAILBOT_GEMINI_KEY_PATH') else None,
            gemini_model=env.get('MAILBOT_GEMINI_MODEL', 'gemini-3.5-flash-lite'),
            gemini_daily_limit=gemini_limit,
            gmail_actions_enabled=actions_flag in {"true", "1"},
            gmail_tasks_enabled=tasks_flag in {'true', '1'},
            gmail_enabled=gmail_flag in {"true", "1"},
            gmail_token_path=_path_from_env(env["MAILBOT_GMAIL_TOKEN_PATH"]) if env.get("MAILBOT_GMAIL_TOKEN_PATH") else None,
            gmail_database_path=_path_from_env(env["MAILBOT_GMAIL_DATABASE_PATH"]) if env.get("MAILBOT_GMAIL_DATABASE_PATH") else None,
            gmail_poll_seconds=gmail_interval,
            database_path=_path_from_env(
                env.get("MAILBOT_DATABASE_PATH", "data/mailbot.sqlite3")
            ),
            image_dir=_path_from_env(env.get("MAILBOT_IMAGE_DIR", "data/images")),
            max_image_bytes=_positive_int(
                env.get("MAILBOT_MAX_IMAGE_BYTES"),
                setting_name="MAILBOT_MAX_IMAGE_BYTES",
                default=8 * 1024 * 1024,
            ),
            discord_token=token,
            allowed_guild_ids=parse_discord_id_list(
                env.get("DISCORD_ALLOWED_GUILDS"),
                setting_name="DISCORD_ALLOWED_GUILDS",
            ),
            allowed_channel_ids=parse_discord_id_list(
                env.get("DISCORD_ALLOWED_CHANNELS"),
                setting_name="DISCORD_ALLOWED_CHANNELS",
            ),
            allowed_user_ids=parse_discord_id_list(
                env.get("DISCORD_ALLOWED_USERS"),
                setting_name="DISCORD_ALLOWED_USERS",
            ),
        )

    @property
    def discord_token_configured(self) -> bool:
        return bool(self.discord_token)

    @property
    def discord_allowlist_configured(self) -> bool:
        return bool(
            self.allowed_guild_ids
            and self.allowed_channel_ids
            and self.allowed_user_ids
        )

    @property
    def reminder_channel_id(self) -> int | None:
        """Use the first explicitly allowed channel for local reminders."""

        return min(self.allowed_channel_ids) if self.allowed_channel_ids else None

    def discord_authorized(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
    ) -> bool:
        """Return whether all three explicitly configured scopes match."""

        if not self.discord_token_configured or not self.discord_allowlist_configured:
            return False
        if guild_id is None or channel_id is None or user_id is None:
            return False
        return (
            guild_id in self.allowed_guild_ids
            and channel_id in self.allowed_channel_ids
            and user_id in self.allowed_user_ids
        )

    def public_dict(self) -> dict[str, object]:
        """Return safe status data without token values."""

        return {
            "database_path": str(self.database_path),
            "image_dir": str(self.image_dir),
            "max_image_bytes": self.max_image_bytes,
            "discord_token_configured": self.discord_token_configured,
            "discord_allowlist_configured": self.discord_allowlist_configured,
            "discord_default_deny": True,
            "allowed_guild_count": len(self.allowed_guild_ids),
            "allowed_channel_count": len(self.allowed_channel_ids),
            "allowed_user_count": len(self.allowed_user_ids),
            "gemini_enabled": self.gemini_enabled,
            "gemini_preview_consent": self.gemini_preview_consent,
            "gemini_daily_limit": self.gemini_daily_limit,
            "gmail_actions_enabled": self.gmail_actions_enabled,
            "gmail_tasks_enabled": self.gmail_tasks_enabled,
            "gmail_enabled": self.gmail_enabled,
            "gmail_token_path_configured": self.gmail_token_path is not None,
            "gmail_poll_seconds": self.gmail_poll_seconds,
            "gmail_multi_account_configured": self.gmail_accounts_path is not None,
            "gmail_trash_enabled": self.gmail_trash_enabled,
            "gmail_task_archive_enabled": self.gmail_task_archive_enabled,
            "todoist_enabled": self.todoist_enabled,
            "todoist_token_path_configured": self.todoist_token_path is not None,
            "todoist_poll_seconds": self.todoist_poll_seconds,
        }
