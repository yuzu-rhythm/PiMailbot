#!/usr/bin/env python3
"""Bootstrap Gmail OAuth with explicit opt-in for label modification."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable
ROOT = Path(__file__).resolve().parents[1]
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MODIFY_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CLIENT_RELATIVE = Path("data/google/client_secret.json")
TOKEN_RELATIVE = Path("data/google/token.json")
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
SUCCESS_MESSAGE = "Gmail authorization completed. You may close this browser window."
class GmailSetupError(RuntimeError):
    pass

def _git(root: Path, arguments: Iterable[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", *arguments], cwd=root, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False)
    except OSError as exc:
        raise GmailSetupError("could not verify Git safety rules") from exc

def _existing_path_is_safe(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GmailSetupError("could not inspect the Gmail credential path") from exc
    if stat.S_ISLNK(info.st_mode):
        raise GmailSetupError("refusing a symlink in the Gmail credential path")
    if not stat.S_ISDIR(info.st_mode) and path.name in {"data", "google"}:
        raise GmailSetupError("Gmail credential parent is not a directory")

def _verify_target(root: Path, relative: Path) -> Path:
    if not root.is_dir():
        raise GmailSetupError("project directory is not available")
    current = root
    for part in relative.parts[:-1]:
        current /= part
        _existing_path_is_safe(current)
    target = root / relative
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise GmailSetupError("could not inspect a Gmail credential target") from exc
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise GmailSetupError("refusing a symlink Gmail credential target")
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise GmailSetupError("Gmail credential target is not a regular file")

    relative_text = relative.as_posix()
    tracked = _git(root, ["ls-files", "--error-unmatch", "--", relative_text])
    if tracked.returncode == 0:
        raise GmailSetupError("refusing a tracked Gmail credential target")
    if tracked.returncode != 1:
        raise GmailSetupError("could not verify whether a Gmail target is tracked")
    ignored = _git(root, ["check-ignore", "--quiet", "--", relative_text])
    if ignored.returncode != 0:
        raise GmailSetupError("Gmail credential targets must be Git-ignored")
    return target

def _private_parent(root: Path) -> Path:
    data = root / "data"
    google = data / "google"
    for directory in (data, google):
        _existing_path_is_safe(directory)
        try:
            directory.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=True)
            if not directory.is_dir() or directory.is_symlink():
                raise GmailSetupError("refusing an unsafe Gmail credential directory")
            os.chmod(directory, PRIVATE_DIR_MODE)
        except OSError as exc:
            raise GmailSetupError("could not create the private Gmail credential directory") from exc
    return google

def _targets(root: Path) -> tuple[Path, Path]:
    client = _verify_target(root, CLIENT_RELATIVE)
    token = _verify_target(root, TOKEN_RELATIVE)
    _private_parent(root)
    return client, token

def _read_client(source: Path) -> bytes:
    try:
        info = source.lstat()
    except OSError as exc:
        raise GmailSetupError("client JSON file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise GmailSetupError("client JSON must be a regular file, not a symlink")
    try:
        raw = source.read_bytes()
        document: Any = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailSetupError("client JSON is not valid JSON") from exc
    installed = document.get("installed") if isinstance(document, dict) else None
    if not isinstance(installed, dict) or "web" in document:
        raise GmailSetupError("client JSON must be an installed desktop OAuth client")
    if installed.get("auth_uri") != AUTH_URI or installed.get("token_uri") != TOKEN_URI:
        raise GmailSetupError("client JSON has an unexpected OAuth endpoint")
    if not isinstance(installed.get("client_id"), str) or not installed["client_id"]:
        raise GmailSetupError("client JSON is missing its desktop client ID")
    if not isinstance(installed.get("client_secret"), str) or not installed["client_secret"]:
        raise GmailSetupError("client JSON is missing its desktop client secret")
    redirects = installed.get("redirect_uris")
    if redirects is not None and (
        not isinstance(redirects, list) or any(uri != "http://localhost" for uri in redirects)
    ):
        raise GmailSetupError("client JSON contains a non-local redirect URI")
    return raw

def _atomic_write(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise GmailSetupError("refusing a symlink Gmail credential target")
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise GmailSetupError("could not write Gmail credentials securely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass

def _authorize(client: Path, *, modify: bool = False, login_hint: str | None = None) -> bytes:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailSetupError("google-auth-oauthlib is required only when running Gmail authorization") from exc
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client), MODIFY_SCOPES if modify else SCOPES)
        credentials = flow.run_local_server(
            host="localhost", bind_addr="127.0.0.1", port=0, timeout_seconds=300,
            access_type="offline",
            prompt="consent",
            authorization_prompt_message="",
            success_message=SUCCESS_MESSAGE,
            **({'login_hint': login_hint} if login_hint else {}),
        )
        serialized = credentials.to_json()
        document = json.loads(serialized)
        required = ('client_id', 'client_secret', 'refresh_token', 'token_uri')
        if not isinstance(document, dict) or any(not document.get(k) for k in required) or document['token_uri'] != TOKEN_URI:
            raise GmailSetupError('Google did not return reusable authorization; retry consent')
        requested = MODIFY_SCOPES if modify else SCOPES
        scopes = document.get('scopes', [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not isinstance(scopes, list) or not set(requested).issubset(scopes):
            raise GmailSetupError('Google did not return the requested Gmail scope')
        if isinstance(serialized, str):
            return serialized.encode("utf-8")
        if isinstance(serialized, bytes):
            return serialized
    except Exception as exc:
        raise GmailSetupError("Gmail authorization failed") from exc
    raise GmailSetupError("Gmail authorization returned invalid credentials")

def bootstrap(project_root: Path, client_source: Path, *, replace: bool = False, modify: bool = False) -> tuple[Path, Path]:
    client_target, token_target = _targets(project_root)
    if token_target.exists() and not replace:
        raise GmailSetupError("a Gmail token already exists; use --replace to authorize again")
    client_bytes = _read_client(client_source)
    _atomic_write(client_target, client_bytes)
    _atomic_write(token_target, _authorize(client_target, modify=True) if modify else _authorize(client_target))
    return client_target, token_target

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Gmail OAuth on this Mac")
    parser.add_argument("--client", required=True, type=Path, help="downloaded desktop OAuth JSON")
    parser.add_argument("--replace", action="store_true", help="replace an existing token after consent")
    parser.add_argument("--modify", action="store_true", help="request Gmail modify scope for read/archive reactions")
    args = parser.parse_args(argv)
    try:
        bootstrap(ROOT, args.client, replace=args.replace, modify=args.modify)
    except KeyboardInterrupt:
        return 130
    except GmailSetupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("Gmail OAuth bootstrap completed; the token is stored locally.")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
