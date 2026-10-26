"""Command line entry point for the offline mailbot prototype."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .attachments import AttachmentError, AttachmentStore
from .config import ConfigurationError, Settings
from .discord_adapter import DiscordConfigurationError, DiscordDependencyUnavailable
from .storage import SqliteStore, StorageError
from .mail_workflow import MailWorkflowError, MailWorkflowStore


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailbot",
        description="オフラインで動作する Discord mailbot プロトタイプ",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", dest="database_path", help="SQLiteファイル（環境変数で上書き可）")
    parser.add_argument("--image-dir", help="画像保存ディレクトリ（環境変数で上書き可）")
    parser.add_argument("--max-image-bytes", type=int, help="画像サイズ上限")
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("demo", help="ローカルデータを作成して状態を表示")
    commands.add_parser("status", help="ローカル状態と外部連携の未設定状態を表示")
    commands.add_parser("reminders", help="期限超過の未送信リマインダーを表示")

    task = commands.add_parser("task", help="ローカルタスク操作")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_add = task_commands.add_parser("add", help="タスクを追加")
    task_add.add_argument("title")
    task_add.add_argument("--due", help="ISO-8601日時。省略時は期限なし")
    task_list = task_commands.add_parser("list", help="タスクを一覧")
    task_list.add_argument("--open-only", action="store_true")
    task_done = task_commands.add_parser("done", help="タスクを完了")
    task_done.add_argument("task_id")
    task_commands.add_parser("export", help="Google Tasks用JSONを表示（送信しない）")

    game = commands.add_parser("game", help="手動ゲームクレジット操作")
    game_commands = game.add_subparsers(dest="game_command", required=True)
    game_credit = game_commands.add_parser("credit", help="クレジットを記録")
    game_credit.add_argument("game")
    game_credit.add_argument("credits", type=int)
    game_credit.add_argument("--note", default="")
    game_list = game_commands.add_parser("list", help="クレジットを一覧")
    game_list.add_argument("--game")

    attachment = commands.add_parser("attachment", help="ローカル画像を保存")
    attachment_commands = attachment.add_subparsers(dest="attachment_command", required=True)
    attachment_save = attachment_commands.add_parser("save", help="画像ファイルを登録")
    attachment_save.add_argument("source", type=Path)
    attachment_save.add_argument("--name", help="保存時の元ファイル名")

    backup = commands.add_parser("backup", help="SQLiteバックアップを作成")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--force", action="store_true", help="既存ファイルを置換")

    commands.add_parser("discord", help="明示設定後にDiscord slash commandを起動")
    mail = commands.add_parser("mail", help="オフラインのメール通知・整理シミュレーション")
    mail.add_argument("--mail-db", default="data/mail-workflow.sqlite3")
    mail_commands = mail.add_subparsers(dest="mail_command", required=True)
    mail_commands.add_parser("demo", help="架空メールからリアクションまでを実行（外部送信なし）")
    ingest = mail_commands.add_parser("ingest", help="ローカルemlを取り込む")
    ingest.add_argument("eml", type=Path)
    ingest.add_argument("--id", required=True, dest="message_id")
    for name in ("status", "notifications", "actions", "deliveries"):
        mail_commands.add_parser(name)
    render = mail_commands.add_parser("render")
    render.add_argument("message_id")
    deliver = mail_commands.add_parser("simulate-delivery")
    deliver.add_argument("message_id")
    react = mail_commands.add_parser("react")
    react.add_argument("delivery_id")
    react.add_argument("emoji", choices=("✅", "📥", "⭐"))
    react.add_argument("--remove", action="store_true")
    for command in (deliver, react):
        command.add_argument("--guild", type=int, required=True)
        command.add_argument("--channel", type=int, required=True)
        command.add_argument("--user", type=int, required=True)
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    changes: dict[str, object] = {}
    if args.database_path:
        changes["database_path"] = Path(args.database_path).expanduser()
    if args.image_dir:
        changes["image_dir"] = Path(args.image_dir).expanduser()
    if args.max_image_bytes is not None:
        if args.max_image_bytes <= 0:
            raise ConfigurationError("--max-image-bytes must be positive")
        changes["max_image_bytes"] = args.max_image_bytes
    return replace(settings, **changes)


def _status_payload(settings: Settings, store: SqliteStore) -> dict[str, object]:
    return {
        "config": settings.public_dict(),
        "storage": store.status(),
        "integrations": {
            "discord": "CONFIGURED_NOT_CONNECTED"
            if settings.discord_token_configured and settings.discord_allowlist_configured
            else "NOT_CONFIGURED",
            "gmail": "NOT_CONFIGURED",
            "google_tasks": "NOT_CONFIGURED",
            "llm": "NOT_CONFIGURED",
        },
    }


def _run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.command == "mail":
        return _run_mail(args, settings)
    if args.command == "discord":
        from .discord_adapter import run_discord

        run_discord(settings)
        return 0

    with SqliteStore(settings.database_path) as store:
        if args.command == "demo":
            task = store.get_task("offline-demo-reminder")
            if task is None:
                task = store.add_task(
                    "オフラインデモの期限超過タスク",
                    due_at="2000-01-01T00:00:00Z",
                    task_id="offline-demo-reminder",
                )
            credits = store.list_game_credits(game="demo")
            if not credits:
                store.add_game_credit("demo", 1, note="オフライン手動記録")
            _json_print(
                {
                    "message": "ローカルデモを保存しました。再起動後も残ります。",
                    "demo_task": task["id"],
                    "pending_reminders": store.pending_reminders(),
                    "status": _status_payload(settings, store),
                }
            )
            return 0

        if args.command == "status":
            _json_print(_status_payload(settings, store))
            return 0

        if args.command == "reminders":
            _json_print(store.pending_reminders())
            return 0

        if args.command == "task":
            if args.task_command == "add":
                _json_print(store.add_task(args.title, due_at=args.due))
            elif args.task_command == "list":
                _json_print(store.list_tasks(include_completed=not args.open_only))
            elif args.task_command == "done":
                _json_print(store.complete_task(args.task_id))
            elif args.task_command == "export":
                from .task_export import task_payload
                _json_print([task_payload(task) for task in store.list_tasks()])
            return 0

        if args.command == "game":
            if args.game_command == "credit":
                _json_print(store.add_game_credit(args.game, args.credits, note=args.note))
            elif args.game_command == "list":
                _json_print(store.list_game_credits(game=args.game))
            return 0

        if args.command == "attachment":
            if args.attachment_command == "save":
                source = args.source.expanduser()
                if not source.is_file():
                    raise FileNotFoundError(f"source is not a file: {source}")
                with source.open("rb") as handle:
                    data = handle.read(settings.max_image_bytes + 1)
                record = AttachmentStore(
                    settings.image_dir,
                    store,
                    max_bytes=settings.max_image_bytes,
                ).save_image(args.name or source.name, data)
                _json_print(record)
            return 0

        if args.command == "backup":
            _json_print(
                {
                    "backup": str(
                        store.backup_to(args.destination, overwrite=args.force)
                    )
                }
            )
            return 0

    # argparse normally catches this, but a future command should not silently
    # return success if it is accidentally left without a handler.
    raise ValueError("command is required")


def _run_mail(args: argparse.Namespace, settings: Settings) -> int:
    # A fixed demo policy applies only to the local fictional scenario.
    # It does not alter runtime Discord settings or connect to any service.
    policy = ({"allowed_guild_ids": {101}, "allowed_channel_ids": {202},
               "allowed_user_ids": {303}} if args.mail_command == "demo" else {})
    with MailWorkflowStore(args.mail_db, settings=settings, **policy) as workflow:
        command = args.mail_command
        if command == "demo":
            workflow.ingest_eml(
                b'Subject: Fictional prototype review\nFrom: demo@example.invalid\n\nPlease review the offline prototype tomorrow.',
                message_id="offline-demo-mail-001",
            )
            delivery = workflow.simulate_delivery("offline-demo-mail-001", 101, 202, user_id=303)
            action = workflow.react(str(delivery["delivery_id"]), "✅", user_id=303, guild_id=101, channel_id=202)
            _json_print({"mode": "SIMULATED", "notification": workflow.render_notification("offline-demo-mail-001"),
                         "delivery": delivery, "action": action, "status": workflow.status()})
        elif command == "ingest":
            _json_print(workflow.ingest_eml(args.eml, message_id=args.message_id))
        elif command == "render":
            _json_print(workflow.render_notification(args.message_id))
        elif command == "simulate-delivery":
            _json_print(workflow.simulate_delivery(args.message_id, args.guild, args.channel, user_id=args.user))
        elif command == "react":
            _json_print(workflow.react(args.delivery_id, args.emoji, user_id=args.user,
                                      guild_id=args.guild, channel_id=args.channel, added=not args.remove))
        else:
            method = {"status": workflow.status, "notifications": workflow.list_notifications,
                      "actions": workflow.list_actions, "deliveries": workflow.list_deliveries}[command]
            _json_print(method())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return _run(args)
    except (
        AttachmentError,
        ConfigurationError,
        DiscordConfigurationError,
        DiscordDependencyUnavailable,
        FileExistsError,
        FileNotFoundError,
        KeyError,
        OSError,
        StorageError,
        MailWorkflowError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        # These are user-actionable local errors.  Secrets are never
        # interpolated here, and token values are never printed.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Do not print arbitrary third-party exception text: future network
        # clients could include request URLs, headers, or response bodies.
        print("error: unexpected local failure (details were not printed)", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
