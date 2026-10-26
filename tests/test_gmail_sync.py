from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from mailbot.gmail_client import (
    GmailAuthError,
    GmailClient,
    GmailTransientError,
    HistoryExpiredError,
)
from mailbot.gmail_sync import GmailSync


class FakeClient:
    def __init__(self) -> None:
        self.profile = {"historyId": "100"}
        self.pages: dict[str | None, dict[str, object]] = {}
        self.messages: dict[str, dict[str, object]] = {}
        self.history_calls: list[tuple[str, str | None]] = []
        self.message_calls: list[str] = []
        self.profile_calls = 0
        self.history_error: Exception | None = None
        self.message_errors: dict[str, Exception] = {}

    def get_profile(self) -> dict[str, object]:
        self.profile_calls += 1
        return self.profile

    def list_history(
        self,
        start_history_id: str,
        *,
        page_token: str | None = None,
        max_results: int = 100,
    ) -> dict[str, object]:
        self.history_calls.append((start_history_id, page_token))
        if self.history_error is not None:
            error, self.history_error = self.history_error, None
            raise error
        return self.pages[page_token]

    def get_message(self, message_id: str, *, format: str = "metadata") -> dict[str, object]:
        self.message_calls.append(message_id)
        if message_id in self.message_errors:
            raise self.message_errors[message_id]
        return self.messages[message_id]


def message(
    message_id: str,
    *,
    inbox: bool = True,
    sender: str = "sender@example.invalid",
    subject: str = "Subject",
    snippet: str = "A bounded snippet",
) -> dict[str, object]:
    labels = ["INBOX"] if inbox else ["SENT"]
    return {
        "id": message_id,
        "labelIds": labels,
        "snippet": snippet,
        "internalDate": "1720000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ]
        },
    }


class GmailSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "private" / "gmail-notifications.sqlite3"
        self.client = FakeClient()
        self.client.pages[None] = {
            "historyId": "101",
            "nextPageToken": "next",
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "m1"}},
                        {"message": {"id": "m2"}},
                        {"message": {"id": "m1"}},
                    ]
                }
            ],
        }
        self.client.pages["next"] = {
            "historyId": "102",
            "history": [{"messagesAdded": [{"message": {"id": "m2"}}, {"message": {"id": "m3"}}]}],
        }
        self.client.messages.update(
            {
                "m1": message("m1", subject="First", snippet="first snippet"),
                "m2": message("m2", inbox=False),
                "m3": message("m3", sender="other@example.invalid", subject="Third"),
            }
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_baseline_pagination_inbox_filter_dedup_and_restart(self) -> None:
        with GmailSync(self.database) as sync:
            self.assertEqual(sync.start(self.client)["state"], "BASELINED")
            self.assertEqual(sync.pending(), [])
            result = sync.poll_once(self.client)
            self.assertEqual(result["state"], "READY")
            self.assertEqual(result["inserted"], 2)
            self.assertEqual(self.client.message_calls, ["m1", "m2", "m3"])
            pending = sync.pending()
            self.assertEqual([item["message_id"] for item in pending], ["m1", "m3"])
            self.assertEqual(pending[0]["sender"], "sender@example.invalid")
            self.assertEqual(pending[0]["subject"], "First")
            self.assertEqual(pending[0]["snippet"], "first snippet")
            self.assertTrue(sync.mark_sent("m1", "discord-1"))
            self.assertFalse(sync.mark_sent("m1", "discord-1-again"))

        with GmailSync(self.database) as reopened:
            self.client.pages[None] = {"historyId": "103", "history": []}
            result = reopened.poll_once(self.client)
            self.assertEqual(result["state"], "READY")
            self.assertEqual(self.client.history_calls[-1], ("102", None))
            self.assertEqual([item["message_id"] for item in reopened.pending()], ["m3"])

    def test_page_limit_persists_page_token_and_resumes(self) -> None:
        with GmailSync(self.database, page_limit=1) as sync:
            sync.start(self.client)
            first = sync.poll_once(self.client)
            self.assertEqual(first["state"], "PARTIAL")
            self.assertTrue(sync.status()["history_page_pending"])
            self.assertEqual(sync.status()["history_id"], "100")
            second = sync.poll_once(self.client)
            self.assertEqual(second["state"], "READY")
            self.assertEqual(self.client.history_calls[-1], ("100", "next"))
            self.assertEqual(sync.status()["history_id"], "102")

    def test_transient_failure_keeps_cursor_and_outbox(self) -> None:
        with GmailSync(self.database) as sync:
            sync.start(self.client)
            self.client.history_error = GmailTransientError("body should not be copied", status=503)
            result = sync.poll_once(self.client)
            self.assertEqual(result["state"], "BACKOFF")
            self.assertEqual(sync.status()["history_id"], "100")
            self.assertEqual(sync.pending(), [])
            self.assertEqual(sync.status()["last_error"], "Gmail service is temporarily unavailable")
            self.assertNotIn("body should not be copied", repr(result))
            self.assertEqual(sync.poll_once(self.client)["state"], "READY")

    def test_history_expiry_requires_explicit_resync_without_cursor_change(self) -> None:
        with GmailSync(self.database) as sync:
            sync.start(self.client)
            self.client.history_error = HistoryExpiredError("provider body", status=404)
            result = sync.poll_once(self.client)
            self.assertEqual(result["state"], "RESYNC_REQUIRED")
            self.assertEqual(sync.status()["history_id"], "100")
            self.assertFalse(sync.status()["history_page_pending"])
            self.assertNotIn("provider body", repr(result))

    def test_deleted_message_is_skipped_and_expired_cursor_can_be_reset_explicitly(self) -> None:
        with GmailSync(self.database) as sync:
            sync.start(self.client)
            from mailbot.gmail_client import GmailError

            self.client.message_errors["m1"] = GmailError("deleted body", status=404)
            result = sync.poll_once(self.client)
            self.assertEqual(result["state"], "READY")
            self.assertEqual([item["message_id"] for item in sync.pending()], ["m3"])
            self.assertEqual(sync.status()["history_id"], "102")
            self.client.profile = {"historyId": "200"}
            self.assertEqual(sync.reset_baseline(self.client)["history_id"], "200")
            self.assertEqual(sync.status()["history_id"], "200")


class _Response:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode()
        self.closed = False

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True

    def getcode(self) -> int:
        return self.status


class GmailClientTests(unittest.TestCase):
    credentials = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "access_token": "old-access-secret",
    }

    def test_401_refreshes_once_and_metadata_headers_are_fixed(self) -> None:
        calls: list[object] = []

        def opener(request: object, *, timeout: float) -> object:
            calls.append(request)
            if len(calls) == 1:
                raise HTTPError(
                    request.full_url,
                    401,
                    "body-token-secret",
                    {},
                    BytesIO(b"response-body-secret"),
                )
            if len(calls) == 2:
                return _Response({"access_token": "new-access-secret"})
            return _Response({"historyId": "42"})

        client = GmailClient(self.credentials, opener=opener)
        self.assertEqual(client.get_profile(), {"historyId": "42"})
        self.assertEqual(calls[0].get_method(), "GET")
        self.assertEqual(calls[1].get_method(), "POST")
        self.assertEqual(calls[2].get_header("Authorization"), "Bearer new-access-secret")

        def metadata_opener(request: object, *, timeout: float) -> object:
            self.assertIn("metadataHeaders=Subject", request.full_url)
            self.assertIn("metadataHeaders=From", request.full_url)
            self.assertTrue(request.full_url.startswith("https://gmail.googleapis.com/"))
            return _Response({"id": "m1"})

        client = GmailClient(self.credentials | {"access_token": "valid"}, opener=metadata_opener)
        self.assertEqual(client.get_message("m1")["id"], "m1")

    def test_invalid_grant_and_429_are_safe_and_retryable(self) -> None:
        def unauthorized(request: object, *, timeout: float) -> object:
            raise HTTPError(request.full_url, 400, "invalid_grant token body", {}, BytesIO(b"secret"))

        client = GmailClient(self.credentials | {"access_token": None}, opener=unauthorized)
        with self.assertRaises(GmailAuthError) as context:
            client.get_profile()
        self.assertEqual(str(context.exception), "Gmail authorization required")
        self.assertNotIn("invalid_grant", str(context.exception))
        self.assertNotIn("secret", repr(context.exception))

        def unavailable(request: object, *, timeout: float) -> object:
            error = HTTPError(request.full_url, 429, "body", {"Retry-After": "4"}, BytesIO(b"secret"))
            raise error

        client = GmailClient(self.credentials, opener=unavailable)
        with self.assertRaises(GmailTransientError) as context:
            client.get_profile()
        self.assertEqual(context.exception.status, 429)
        self.assertEqual(context.exception.retry_after, 4.0)
        self.assertNotIn("body", str(context.exception))

    def test_token_endpoint_is_allowlisted(self) -> None:
        with self.assertRaises(GmailAuthError):
            GmailClient(self.credentials | {"token_uri": "https://evil.invalid/token"})

    def test_token_path_requires_private_regular_file_and_private_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            token_path = root / "token.json"
            token_path.write_text(json.dumps(self.credentials), encoding="utf-8")
            token_path.chmod(0o600)
            self.assertEqual(GmailClient(token_path).auth_state, "AUTHORIZED")
            token_path.chmod(0o644)
            with self.assertRaises(GmailAuthError):
                GmailClient(token_path)


if __name__ == "__main__":
    unittest.main()
