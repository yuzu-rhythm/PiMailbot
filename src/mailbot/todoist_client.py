"""Todoist API v1 client. Credential values and response bodies never enter errors."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener

from .gmail_client import _RejectRedirect


class TodoistError(RuntimeError):
    def __init__(self, code, *, ambiguous=False, retry_after=90):
        super().__init__(code)
        self.code, self.ambiguous, self.retry_after = code, ambiguous, retry_after


class TodoistClient:
    def __init__(self, token_path: Path, *, opener=None):
        try:
            info, parent = token_path.lstat(), token_path.parent.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                    not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) & 0o077):
                raise TodoistError('TOKEN_PERMISSIONS')
            token = token_path.read_text().strip()
            if not re.fullmatch(r'[A-Za-z0-9_.~-]{20,512}', token):
                raise TodoistError('INVALID_TOKEN')
        except OSError:
            raise TodoistError('TOKEN_UNAVAILABLE') from None
        self._token = token
        self._opener = opener or build_opener(_RejectRedirect()).open

    def _request(self, path, *, body=None, request_id=None):
        headers = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json',
                   'Content-Type': 'application/json', 'User-Agent': 'PiMailbot/0.1'}
        if request_id:
            headers['X-Request-Id'] = request_id
        request = Request('https://api.todoist.com/api/v1' + path, headers=headers,
                          method='POST' if body is not None else 'GET',
                          data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None)
        try:
            with self._opener(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise TodoistError('INVALID_RESPONSE', ambiguous=body is not None)
                if response.status == 204:
                    return {}
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TodoistError('INVALID_RESPONSE', ambiguous=body is not None)
            return payload
        except HTTPError as error:
            code = error.code
            try:
                retry = min(3600, max(30, int(error.headers.get('Retry-After', 90))))
            except (ValueError, TypeError, AttributeError):
                retry = 90
            error.close()
            if code in (401, 403):
                raise TodoistError('AUTH_REQUIRED') from None
            if code == 404:
                raise TodoistError('REMOTE_MISSING') from None
            if code == 429:
                raise TodoistError('RATE_LIMITED', retry_after=retry) from None
            raise TodoistError('API_ERROR', ambiguous=code == 409 or code >= 500) from None
        except TodoistError:
            raise
        except Exception:
            raise TodoistError('REQUEST_FAILED', ambiguous=body is not None) from None

    def create_task(self, task, *, request_id, project_id=None):
        content = str(task['title'])[:500]
        # Escape Todoist markdown to preserve literal email subjects.
        content = re.sub(r'([\\`*_\[\]()~])', r'\\\1', content)
        body = {'content': content, 'description': 'Pi Mailbot reference: ' + str(task['id'])}
        if project_id:
            body['project_id'] = project_id
        if task.get('due_at') is not None:
            body['due_datetime'] = datetime.fromtimestamp(task['due_at'], timezone.utc).isoformat()
        result = self._request('/tasks', body=body, request_id=request_id)
        remote_id = result.get('id')
        if not isinstance(remote_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', remote_id):
            raise TodoistError('INVALID_RESPONSE', ambiguous=True)
        return remote_id

    def close_task(self, remote_id, *, request_id):
        self._request('/tasks/' + quote(remote_id, safe='') + '/close', body={}, request_id=request_id)

    def task_completed(self, remote_id):
        result = self._request('/tasks/' + quote(remote_id, safe=''))
        for field in ('is_completed', 'checked'):
            if isinstance(result.get(field), bool):
                return result[field]
        raise TodoistError('INVALID_RESPONSE')

    def check_connection(self):
        """Read-only account check. Do not log the returned private data."""
        result = self._request('/projects?limit=1')
        if not isinstance(result.get('results'), list):
            raise TodoistError('INVALID_RESPONSE')
        return True
