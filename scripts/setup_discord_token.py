#!/usr/bin/env python3
"""Store a Discord bot token in the local, ignored environment file.

The token is intentionally collected with :func:`getpass.getpass` from an
interactive terminal.  This module does not contact Discord or attempt to
validate the token remotely.  The command only changes ``.env.local`` in the
selected project directory, preserving the other settings in that file.
"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import warnings
from typing import Callable, Iterable, TextIO


TOKEN_ENV_NAME = "DISCORD_BOT_TOKEN"
ENV_FILENAME = ".env.local"
PRIVATE_MODE = 0o600
TOKEN_ALLOWED_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class TokenSetupError(RuntimeError):
    """A safe, user-actionable local setup failure."""


def _target_path(project_root: Path) -> Path:
    """Return the only file this helper is allowed to modify."""

    return project_root / ENV_FILENAME


def _run_git_check(
    project_root: Path,
    arguments: Iterable[str],
) -> subprocess.CompletedProcess[bytes]:
    """Run a quiet, read-only Git check without exposing command output."""

    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise TokenSetupError("could not verify the local Git safety rules") from exc


def _verify_destination(project_root: Path) -> Path:
    """Check that ``.env.local`` is ignored, untracked, and not a symlink."""

    if not project_root.is_dir():
        raise TokenSetupError("project directory is not available")

    target = _target_path(project_root)
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        target_stat = None
    except OSError as exc:
        raise TokenSetupError("could not inspect the local environment file") from exc

    if target_stat is not None:
        if stat.S_ISLNK(target_stat.st_mode):
            raise TokenSetupError("refusing a symlink environment file")
        if not stat.S_ISREG(target_stat.st_mode):
            raise TokenSetupError("environment path is not a regular file")

    tracked = _run_git_check(
        project_root,
        ["ls-files", "--error-unmatch", "--", ENV_FILENAME],
    )
    if tracked.returncode == 0:
        raise TokenSetupError("refusing a tracked environment file")
    if tracked.returncode != 1:
        raise TokenSetupError("could not verify whether the environment file is tracked")

    ignored = _run_git_check(
        project_root,
        ["check-ignore", "--quiet", "--", ENV_FILENAME],
    )
    if ignored.returncode == 1:
        raise TokenSetupError(".env.local is not ignored by Git")
    if ignored.returncode != 0:
        raise TokenSetupError("could not verify that .env.local is ignored by Git")

    return target


def _read_env_file(target: Path) -> str:
    """Read the environment file without ever including its contents in errors."""

    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise TokenSetupError("could not inspect the local environment file") from exc
    if stat.S_ISLNK(target_stat.st_mode):
        raise TokenSetupError("refusing a symlink environment file")
    if not stat.S_ISREG(target_stat.st_mode):
        raise TokenSetupError("environment path is not a regular file")
    try:
        with target.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise TokenSetupError(".env.local is not valid UTF-8") from exc
    except OSError as exc:
        raise TokenSetupError("could not read .env.local") from exc


def _line_ending(line: str) -> tuple[str, str]:
    """Split a line into body and its original newline sequence."""

    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def _assignment_prefix_and_value(line: str) -> tuple[str, str, str] | None:
    """Return ``(prefix, value, newline)`` for a token assignment line.

    Leading whitespace and an optional ``export`` are accepted so the helper
    can preserve ordinary shell-style ``.env.local`` files.  The parser is
    deliberately limited to identifying this one assignment; it does not
    interpret arbitrary dotenv syntax or print any existing value.
    """

    body, newline = _line_ending(line)
    left, separator, value = body.partition("=")
    if not separator:
        return None
    key = left.strip()
    if key.startswith("export"):
        remainder = key[len("export") :]
        if not remainder or not remainder[0].isspace():
            return None
        key = remainder.strip()
    if key != TOKEN_ENV_NAME:
        return None
    return left + separator, value, newline


def _value_is_configured(value: str) -> bool:
    """Return whether an assignment contains a non-empty configured value."""

    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "'\"":
        candidate = candidate[1:-1].strip()
    return bool(candidate)


def _token_is_configured(content: str) -> bool:
    """Detect an existing token assignment without returning its value."""

    return any(
        parsed is not None and _value_is_configured(parsed[1])
        for parsed in (
            _assignment_prefix_and_value(line)
            for line in content.splitlines(keepends=True)
        )
    )


def _replace_token_assignment(content: str, token: str) -> str:
    """Replace every token assignment while preserving all other file text."""

    lines = content.splitlines(keepends=True)
    replaced = False
    output: list[str] = []
    for line in lines:
        parsed = _assignment_prefix_and_value(line)
        if parsed is None:
            output.append(line)
            continue
        prefix, _old_value, newline = parsed
        output.append(f"{prefix}{token}{newline}")
        replaced = True

    if not replaced:
        newline = "\r\n" if "\r\n" in content else "\n"
        if output and not (output[-1].endswith("\n") or output[-1].endswith("\r")):
            output[-1] += newline
        output.append(f"{TOKEN_ENV_NAME}={token}{newline}")
    return "".join(output)


def validate_token(token: str) -> str:
    """Validate a token locally without network access or value-bearing errors."""

    if not isinstance(token, str) or not token:
        raise TokenSetupError("token is empty")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in token
    ):
        raise TokenSetupError("token contains whitespace or control characters")
    if any(character not in TOKEN_ALLOWED_CHARACTERS for character in token):
        raise TokenSetupError("token contains unsupported characters")
    return token


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` beside the destination, then atomically replace it."""

    temporary_path: Path | None = None
    file_descriptor: int | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            dir=target.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(file_descriptor, PRIVATE_MODE)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            file_descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        # Recheck immediately before replacement so a symlink introduced after
        # the initial preflight is never followed or used as the destination.
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and stat.S_ISLNK(target_stat.st_mode):
            raise TokenSetupError("refusing a symlink environment file")
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            raise TokenSetupError("environment path is not a regular file")

        os.replace(temporary_path, target)
        temporary_path = None
        os.chmod(target, PRIVATE_MODE)
    except TokenSetupError:
        raise
    except OSError as exc:
        raise TokenSetupError("could not write .env.local securely") from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def save_token(project_root: Path, token: str, *, replace: bool = False) -> Path:
    """Save a synthetic or already-collected token for callers and tests.

    The command line entry point always collects its value through
    ``getpass.getpass``.  This lower-level function exists so the file-writing
    behavior can be tested without placing a live secret in process input.
    """

    target = _verify_destination(project_root)
    content = _read_env_file(target)
    if not replace and _token_is_configured(content):
        raise TokenSetupError("a Discord token is already configured; use --replace")
    validated = validate_token(token)
    _atomic_write(target, _replace_token_assignment(content, validated))
    return target


def _require_tty(stdin: TextIO, stderr: TextIO) -> None:
    """Refuse input streams that could echo a secret into a pipe or log."""

    try:
        input_is_tty = stdin.isatty()
        output_is_tty = stderr.isatty()
    except (AttributeError, OSError) as exc:
        raise TokenSetupError("an interactive terminal is required for token input") from exc
    if not input_is_tty or not output_is_tty:
        raise TokenSetupError("an interactive terminal is required for token input")


def configure_token(
    project_root: Path,
    *,
    replace: bool = False,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    prompt_reader: Callable[[str], str] | None = None,
) -> Path:
    """Prompt for a token in a real terminal and save it locally."""

    input_stream = sys.stdin if stdin is None else stdin
    error_stream = sys.stderr if stderr is None else stderr
    target = _verify_destination(project_root)
    content = _read_env_file(target)
    if not replace and _token_is_configured(content):
        raise TokenSetupError("a Discord token is already configured; use --replace")
    _require_tty(input_stream, error_stream)
    reader = getpass.getpass if prompt_reader is None else prompt_reader
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = reader("Discord bot token: ")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning) as exc:
        raise TokenSetupError("token input was cancelled") from exc
    return save_token(project_root, token, replace=replace)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Store a Discord bot token in the local ignored .env.local file."
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing configured token after prompting",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        help="project directory (defaults to the directory containing this script)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the safe local setup command and return a shell exit status."""

    args = _parser().parse_args(argv)
    project_root = (
        args.project_root.expanduser().resolve()
        if args.project_root is not None
        else Path(__file__).resolve().parents[1]
    )
    try:
        configure_token(project_root, replace=args.replace)
    except TokenSetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Discord token saved to .env.local.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
