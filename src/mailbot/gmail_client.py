"""Gmail REST client with narrowly scoped label-removal actions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import stat
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URIS = frozenset(("https://accounts.google.com/o/oauth2/token", "https://oauth2.googleapis.com/token"))
_MAX_BODY = 8 * 1024 * 1024


class GmailError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


class GmailAuthError(GmailError):
    state = "AUTH_REQUIRED"


class GmailTransientError(GmailError):
    retryable = True


class HistoryExpiredError(GmailError):
    state = "RESYNC_REQUIRED"


class GmailProtocolError(GmailError):
    pass


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, newurl: str, code: int, msg: str, headers: Any, fp: Any = None) -> Request:
        raise HTTPError(request.full_url, code, "redirect rejected", headers, fp)


_SAFE_OPENER = build_opener(_RejectRedirect()).open


def _close_error(error: HTTPError) -> None:
    close = getattr(error, "close", None)
    if callable(close):
        close()
        return
    close = getattr(getattr(error, "fp", None), "close", None)
    if callable(close):
        close()


def _token_uri(value: object) -> str:
    if not isinstance(value, str) or value not in _TOKEN_URIS:
        raise ValueError("OAuth token endpoint is not approved")
    return value


def _credentials(source: Mapping[str, object] | str | Path) -> dict[str, object]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            fs, parent = path.lstat(), path.parent.lstat()
            private = stat.S_ISREG(fs.st_mode) and stat.S_IMODE(fs.st_mode) == 0o600
            private = private and stat.S_ISDIR(parent.st_mode) and not (stat.S_IMODE(parent.st_mode) & 0o077)
            if not private:
                raise GmailAuthError("OAuth credentials are unavailable")
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except GmailAuthError:
            raise
        except (OSError, ValueError) as exc:
            raise GmailAuthError("OAuth credentials are unavailable") from exc
    else:
        value = dict(source)
    if not isinstance(value, dict):
        raise GmailAuthError("OAuth credentials are unavailable")
    required = ("client_id", "client_secret", "refresh_token", "token_uri")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise GmailAuthError("OAuth credentials are incomplete")
    try:
        _token_uri(value["token_uri"])
    except ValueError as exc:
        raise GmailAuthError("OAuth token endpoint is not approved") from exc
    return value


def _retry_after(headers: Any) -> float | None:
    try:
        value = float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None
    return min(value, 3600.0) if value >= 0 else None


class GmailClient:
    """Read Gmail and optionally mark individual messages read or archived."""

    def __init__(self, token_source: Mapping[str, object] | str | Path, *, timeout: float = 15.0, opener: Callable[..., Any] | None = None) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._credentials, self._timeout = _credentials(token_source), float(timeout)
        self._opener = opener or _SAFE_OPENER
        token = self._credentials.get("access_token")
        self._access_token = token if isinstance(token, str) and token else None
        self.auth_state = "AUTHORIZED" if self._access_token else "REFRESH_REQUIRED"

    def get_profile(self) -> dict[str, object]:
        return self._get_json("/profile")

    def list_history(self, start_history_id: str, *, page_token: str | None = None, max_results: int = 100) -> dict[str, object]:
        params: dict[str, object] = {"startHistoryId": str(start_history_id), "historyTypes": "messageAdded", "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        try:
            return self._get_json("/history", params)
        except GmailError as exc:
            if exc.status == 404:
                raise HistoryExpiredError("Gmail history cursor expired", status=404) from exc
            raise

    def get_message(self, message_id: str, *, format: str = "metadata", metadata_headers: tuple[str, ...] = ("Subject", "From")) -> dict[str, object]:
        if format != "metadata":
            raise ValueError("only metadata format is supported")
        return self._get_json(f"/messages/{quote(str(message_id), safe='')}", {"format": format, "metadataHeaders": list(metadata_headers)})

    def list_messages(self, *, query: str, page_token: str | None = None, max_results: int = 100) -> dict[str, object]:
        params: dict[str, object] = {"q": query, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return self._get_json("/messages", params)

    @property
    def can_modify(self) -> bool:
        scopes = self._credentials.get("scopes", [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        return isinstance(scopes, (list, tuple)) and GMAIL_MODIFY_SCOPE in scopes

    def apply_action(self, message_id: str, action: str) -> dict[str, object]:
        labels = {"read": "UNREAD", "archive": "INBOX"}
        if action not in {*labels, 'trash'}:
            raise ValueError("Unsupported Gmail action")
        if not self.can_modify:
            raise GmailAuthError("Gmail modify authorization required")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            raise ValueError("Invalid Gmail message identity")
        if action == 'trash':
            return self._get_json(f"/messages/{quote(message_id, safe='')}/trash", body=b'')
        return self._get_json(f"/messages/{quote(message_id, safe='')}/modify",
                              body={"removeLabelIds": [labels[action]]})

    def rename_label(self, label_id: str, name: str) -> dict[str, object]:
        if not self.can_modify or not isinstance(label_id, str) or not label_id or not name:
            raise ValueError('LABEL_RENAME_NOT_ALLOWED')
        return self._get_json('/labels/' + quote(label_id, safe=''), body={'name': name}, method='PATCH')

    def _get_json(self, path: str, params: Mapping[str, object] | None = None, *, body: dict | bytes | None = None, method: str | None = None) -> dict[str, object]:
        token = self._ensure_token()
        request = self._request(path, params, token, body, method)
        try:
            return self._open_json(request)
        except HTTPError as exc:
            if exc.code == 401:
                _close_error(exc)
                self._refresh()
                try:
                    return self._open_json(self._request(path, params, self._ensure_token(), body, method))
                except HTTPError as second:
                    _close_error(second)
                    raise self._http_error(second) from None
            _close_error(exc)
            raise self._http_error(exc) from None

    def _ensure_token(self) -> str:
        if not self._access_token:
            self._refresh()
        if not self._access_token:
            raise GmailAuthError("Gmail authorization required")
        return self._access_token

    def _refresh(self) -> None:
        request = Request(
            _token_uri(self._credentials["token_uri"]),
            data=urlencode({"client_id": self._credentials["client_id"], "client_secret": self._credentials["client_secret"], "refresh_token": self._credentials["refresh_token"], "grant_type": "refresh_token"}).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            payload = self._open_json(request)
        except HTTPError as exc:
            _close_error(exc)
            if exc.code == 429 or 500 <= exc.code <= 599:
                self.auth_state = "BACKOFF"
                raise self._http_error(exc) from None
            self.auth_state = "AUTH_REQUIRED"
            raise GmailAuthError("Gmail authorization required", status=exc.code) from None
        except (URLError, TimeoutError, OSError) as exc:
            self.auth_state = "AUTH_REQUIRED"
            raise GmailAuthError("Gmail authorization required") from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            self.auth_state = "AUTH_REQUIRED"
            raise GmailAuthError("Gmail authorization required")
        self._access_token, self.auth_state = token, "AUTHORIZED"

    @staticmethod
    def _request(path: str, params: Mapping[str, object] | None, token: str, body: dict | bytes | None = None, method: str | None = None) -> Request:
        query = urlencode(params or {}, doseq=True)
        url = f"{_ROOT}{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return Request(url, headers=headers, method=method or ("POST" if body is not None else "GET"),
                       data=body if isinstance(body, bytes) else json.dumps(body).encode("utf-8") if body is not None else None)

    def _open_json(self, request: Request) -> dict[str, object]:
        try:
            response = self._opener(request, timeout=self._timeout)
            try:
                raw = response.read(_MAX_BODY + 1)
                status = getattr(response, "status", None)
                status = response.getcode() if status is None else status
                if len(raw) > _MAX_BODY:
                    raise GmailProtocolError("Gmail response is too large")
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError) as exc:
            raise GmailTransientError("Gmail network request failed") from exc
        if status < 200 or status >= 300:
            raise GmailError("Gmail request failed", status=status)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GmailProtocolError("Gmail returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise GmailProtocolError("Gmail returned an invalid response")
        return value

    @staticmethod
    def _http_error(error: HTTPError) -> GmailError:
        status = getattr(error, "code", None)
        if status == 401:
            return GmailAuthError("Gmail authorization required", status=401)
        if status == 429 or isinstance(status, int) and 500 <= status <= 599:
            return GmailTransientError("Gmail service is temporarily unavailable", status=status, retry_after=_retry_after(getattr(error, "headers", None)))
        return GmailError("Gmail request failed", status=status)
