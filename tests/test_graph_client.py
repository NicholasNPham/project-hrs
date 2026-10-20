"""Tests for graph_client against fake Graph responses. No network.

GraphClient is built without __init__ and given a fake MSAL app and a fake
HTTP session that answers from a queue, so each test says exactly what
Graph returns.
"""

import base64
import sys
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph_client  # noqa: E402
from exceptions import SystemProblem  # noqa: E402
from graph_client import MAX_PAGES, MAX_RETRIES, GraphClient  # noqa: E402
from logger import get_logger  # noqa: E402

MAILBOX = "mailbox@example.com"
SENDER = "sender@example.com"
PHRASE = "Daily Bookings"
DAY = date(2026, 1, 5)
SHEET = b"fake xlsx bytes"


class FakeResponse:
    """The parts of a requests.Response that graph_client reads."""

    def __init__(self, status: int, payload: dict | None = None, headers: dict | None = None) -> None:
        """Hold a status, a JSON payload and headers."""
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        """Return the payload."""
        return self._payload


class FakeSession:
    """Answers each GET with the next queued response and records the call."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        """Queue the responses to give, in order."""
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, headers: dict, params: dict | None, timeout: int) -> FakeResponse:
        """Record the URL and params, then give the next response."""
        self.calls.append((url, params))
        return self.responses.pop(0)


class FakeMsal:
    """Gives the same token answer every time."""

    def __init__(self, answer: dict) -> None:
        """Remember the answer."""
        self.answer = answer

    def acquire_token_for_client(self, scopes: list[str]) -> dict:
        """Return the answer."""
        return self.answer


def _client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[FakeResponse],
    token: dict | None = None,
) -> tuple[GraphClient, FakeSession, list[float]]:
    """A client with fake MSAL and session, and sleeps recorded instead of taken."""
    sleeps: list[float] = []
    monkeypatch.setattr(graph_client.time, "sleep", sleeps.append)
    client = GraphClient.__new__(GraphClient)
    client._msal_app = FakeMsal(token or {"access_token": "placeholder"})
    client._session = FakeSession(responses)
    client._base = f"{graph_client.GRAPH_BASE_URL}/users/{MAILBOX}"
    client._log = get_logger()
    return client, client._session, sleeps


def _message(message_id: str, subject: str | None = f"{PHRASE} report") -> dict:
    """One message as Graph lists it."""
    return {"id": message_id, "subject": subject, "hasAttachments": True}


def _file(name: str, content: bytes = SHEET, inline: bool = False) -> dict:
    """One file attachment as Graph returns it."""
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": name,
        "isInline": inline,
        "contentBytes": base64.b64encode(content).decode() if content else "",
    }


def _listing(*messages: dict, next_link: str | None = None) -> FakeResponse:
    """A page of messages, with a nextLink when there is more."""
    payload: dict = {"value": list(messages)}
    if next_link:
        payload["@odata.nextLink"] = next_link
    return FakeResponse(200, payload)


def _attachments(*files: dict) -> FakeResponse:
    """The attachments of one message."""
    return FakeResponse(200, {"value": list(files)})


def _fetch(client: GraphClient) -> bytes:
    """Fetch the sheet with the test's day, sender and phrase."""
    return client.fetch_booking_sheet(DAY, SENDER, PHRASE)


# Finding the email


def test_one_email_with_one_sheet_returns_its_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path decodes the attachment."""
    client, session, _ = _client(monkeypatch, [
        _listing(_message("m1")),
        _attachments(_file("bookings.xlsx")),
    ])

    assert _fetch(client) == SHEET
    assert session.calls[1][0].endswith("/messages/m1/attachments")


def test_listing_asks_for_the_inbox_sender_and_whole_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """The query names the Inbox, the sender, and midnight to midnight local time."""
    client, session, _ = _client(monkeypatch, [
        _listing(_message("m1")),
        _attachments(_file("bookings.xlsx")),
    ])
    _fetch(client)

    url, params = session.calls[0]
    start = datetime.combine(DAY, clock.min).astimezone().astimezone(timezone.utc)
    end = start + timedelta(days=1)
    assert url == f"{graph_client.GRAPH_BASE_URL}/users/{MAILBOX}/mailFolders/inbox/messages"
    assert params["$filter"] == (
        f"receivedDateTime ge {start:%Y-%m-%dT%H:%M:%SZ} and "
        f"receivedDateTime lt {end:%Y-%m-%dT%H:%M:%SZ} and "
        f"from/emailAddress/address eq '{SENDER}'"
    )
    assert params["$orderby"] == "receivedDateTime desc"


def test_sender_is_lowercased_and_quotes_doubled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quote in the address cannot break out of the Graph string literal."""
    client, session, _ = _client(monkeypatch, [
        _listing(_message("m1")),
        _attachments(_file("bookings.xlsx")),
    ])
    client.fetch_booking_sheet(DAY, " O'Doe@Example.com ", PHRASE)

    assert session.calls[0][1]["$filter"].endswith("address eq 'o''doe@example.com'")


def test_subject_match_ignores_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The phrase matches whatever case the subject uses."""
    client, _, _ = _client(monkeypatch, [
        _listing(_message("m1", subject=f"FW: {PHRASE.upper()}")),
        _attachments(_file("bookings.xlsx")),
    ])

    assert _fetch(client) == SHEET


def test_other_subjects_from_the_sender_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mail from the same sender without the phrase does not count."""
    client, session, _ = _client(monkeypatch, [
        _listing(_message("m0", subject="Something else"), _message("m1"), _message("m2", subject=None)),
        _attachments(_file("bookings.xlsx")),
    ])

    assert _fetch(client) == SHEET
    assert session.calls[1][0].endswith("/messages/m1/attachments")


def test_no_matching_email_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """No email today is a stop, not an empty run."""
    client, _, _ = _client(monkeypatch, [_listing(_message("m0", subject="Something else"))])

    with pytest.raises(SystemProblem, match="found 0"):
        _fetch(client)


def test_two_matching_emails_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resend is not guessed between."""
    client, _, _ = _client(monkeypatch, [_listing(_message("m1"), _message("m2"))])

    with pytest.raises(SystemProblem, match="found 2"):
        _fetch(client)


def test_paging_follows_next_link_without_resending_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """A match on the second page is found, and nextLink is used as given."""
    client, session, _ = _client(monkeypatch, [
        _listing(_message("m0", subject="Something else"), next_link="https://next.example/page2"),
        _listing(_message("m1")),
        _attachments(_file("bookings.xlsx")),
    ])

    assert _fetch(client) == SHEET
    assert session.calls[1] == ("https://next.example/page2", None)


def test_paging_that_never_ends_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nextLink on every page stops after MAX_PAGES instead of looping."""
    pages = [_listing(next_link="https://next.example/more") for _ in range(MAX_PAGES)]
    client, _, _ = _client(monkeypatch, pages)

    with pytest.raises(SystemProblem, match="pages"):
        _fetch(client)


# Picking the attachment


def test_only_real_xlsx_files_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inline files, other types and attached emails are passed over."""
    client, _, _ = _client(monkeypatch, [
        _listing(_message("m1")),
        _attachments(
            _file("logo.xlsx", content=b"inline", inline=True),
            _file("notes.pdf", content=b"pdf"),
            {"@odata.type": "#microsoft.graph.itemAttachment", "name": "fwd.xlsx"},
            _file("BOOKINGS.XLSX"),
        ),
    ])

    assert _fetch(client) == SHEET


def test_no_xlsx_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """An email without a spreadsheet stops the run."""
    client, _, _ = _client(monkeypatch, [
        _listing(_message("m1")),
        _attachments(_file("bookings.xls"), _file("bookings.csv")),
    ])

    with pytest.raises(SystemProblem, match="found 0"):
        _fetch(client)


def test_two_xlsx_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two spreadsheets on the email are not guessed between."""
    client, _, _ = _client(monkeypatch, [
        _listing(_message("m1")),
        _attachments(_file("a.xlsx"), _file("b.xlsx")),
    ])

    with pytest.raises(SystemProblem, match="found 2"):
        _fetch(client)


def test_empty_attachment_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """An .xlsx with no content is refused rather than read as an empty sheet."""
    client, _, _ = _client(monkeypatch, [
        _listing(_message("m1")),
        _attachments(_file("bookings.xlsx", content=b"")),
    ])

    with pytest.raises(SystemProblem, match="no content"):
        _fetch(client)


# Graph errors and retries


@pytest.mark.parametrize("status, text", [
    (401, "rejected the sign-in token"),
    (403, "Mail.Read"),
    (404, "graph.mailbox"),
    (500, "failed with 500"),
])
def test_errors_stop_without_retry(monkeypatch: pytest.MonkeyPatch, status: int, text: str) -> None:
    """Errors a retry cannot fix stop on the first answer."""
    client, session, sleeps = _client(monkeypatch, [FakeResponse(status)])

    with pytest.raises(SystemProblem, match=text):
        _fetch(client)
    assert len(session.calls) == 1
    assert sleeps == []


def test_throttling_is_retried_honoring_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 waits as long as Graph asks, then carries on."""
    client, _, sleeps = _client(monkeypatch, [
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(503),
        _listing(_message("m1")),
        _attachments(_file("bookings.xlsx")),
    ])

    assert _fetch(client) == SHEET
    assert sleeps == [7, 2]


def test_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge Retry-After cannot stall the run."""
    client, _, sleeps = _client(monkeypatch, [
        FakeResponse(429, headers={"Retry-After": "86400"}),
        _listing(_message("m1")),
        _attachments(_file("bookings.xlsx")),
    ])
    _fetch(client)

    assert sleeps == [graph_client.MAX_BACKOFF_SECONDS]


def test_retries_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Graph that stays down stops after MAX_RETRIES tries."""
    client, session, _ = _client(monkeypatch, [FakeResponse(503) for _ in range(MAX_RETRIES)])

    with pytest.raises(SystemProblem, match="503"):
        _fetch(client)
    assert len(session.calls) == MAX_RETRIES


def test_refused_token_names_the_error_code_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code says what is wrong; Microsoft's description is not echoed."""
    client, session, _ = _client(
        monkeypatch, [], token={"error": "invalid_client", "error_description": "secret detail"}
    )

    with pytest.raises(SystemProblem, match="invalid_client") as caught:
        _fetch(client)
    assert "secret detail" not in str(caught.value)
    assert session.calls == []


# What reaches the console and logs


def test_messages_leave_out_address_sender_and_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop messages never repeat the mailbox, sender or phrase."""
    client, _, _ = _client(monkeypatch, [_listing(_message("m1"), _message("m2"))])

    with pytest.raises(SystemProblem) as caught:
        _fetch(client)
    for private in (MAILBOX, SENDER, PHRASE):
        assert private.lower() not in str(caught.value).lower()
