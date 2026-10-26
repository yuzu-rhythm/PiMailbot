#!/usr/bin/env python3
"""Run Discord using local settings without sourcing executable shell code."""
from pathlib import Path
import os
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def read_settings(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Private .env.local file is missing or is a symlink')
    if path.stat().st_mode & 0o077:
        raise ValueError('.env.local permissions must be 0600')
    settings = dict(os.environ)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, raw = line.partition('=')
        key = key.strip()
        if not separator or not (key.startswith('MAILBOT_') or key.startswith('DISCORD_')):
            raise ValueError('Unsupported local configuration entry')
        values = shlex.split(raw, comments=True)
        if len(values) > 1:
            raise ValueError('Invalid local configuration value')
        settings[key] = values[0] if values else ''
    return settings


def main() -> int:
    from mailbot.config import Settings
    from mailbot.discord_adapter import DiscordConfigurationError, DiscordDependencyUnavailable, run_discord
    try:
        settings = Settings.from_env(read_settings(ROOT / '.env.local'))
        if not settings.discord_token_configured:
            print('先に python3 scripts/setup_discord_token.py でトークンを保存してください。')
            return 2
        os.chdir(ROOT)
        run_discord(settings)
        return 0
    except (DiscordConfigurationError, DiscordDependencyUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('起動できませんでした。設定・依存・Discord接続を確認してください（秘密情報は非表示）。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
