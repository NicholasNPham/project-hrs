"""Fetch the day's booking spreadsheet from one mailbox through Microsoft Graph.

Ported from PROJECT-DALYN's graph_client, cut down to reading. HRS signs in
as DALYN's app registration, which has mail access wider than one mailbox,
so every URL is built for the single configured mailbox and nothing else.
HRS never changes the email: it stays in the Inbox, unread or read as a
person left it, and a rerun the same day finds it again.

No address, subject or file name goes into a message or log line, since
they reach the console and the logs.
"""

import base64
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock

import msal
import requests

from exceptions import SystemProblem
from logger import get_logger

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Only the Inbox is read. Searching every folder would also find copies in
# Sent Items or Deleted Items and count them as a second booking email.
FOLDER = "inbox"

# One sender sends a handful of emails a day at most. More pages than this
# means the filter is not doing what it should.
PAGE_SIZE = 50
MAX_PAGES = 10

MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 60

# Microsoft is throttling or briefly down. Only GETs are ever sent, so any
# of these is safe to repeat.
RETRY_STATUSES = frozenset({429, 502, 503, 504})


class GraphClient:
    """Signs in as the Graph app and reads one mailbox's Inbox."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailbox: str) -> None:
        """Build the MSAL client. Does not contact Microsoft until a token is needed.

        Raises:
            SystemProblem: If MSAL rejects the tenant or client id as malformed.
        """
        try:
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=client_id,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
                client_credential=client_secret,
            )
        except ValueError as error:
            raise SystemProblem(
                "Could not set up the Graph sign-in. Check graph.tenant_id and "
                "graph.client_id in config.yaml."
            ) from error
        self._session = requests.Session()
        self._base = f"{GRAPH_BASE_URL}/users/{mailbox.strip().lower()}"
        self._log = get_logger()

    def _get_token(self) -> str:
        """Return an access token, reusing MSAL's cached one until it nears expiry.

        Raises:
            SystemProblem: If Microsoft cannot be reached or refuses the app.
        """
        try:
            result = self._msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        except (requests.ConnectionError, requests.Timeout) as error:
            raise SystemProblem(
                f"Could not reach Microsoft for a Graph token: {type(error).__name__}"
            ) from error
        if "access_token" in result:
            return result["access_token"]
        # The error code says what is wrong (for example invalid_client for a
        # bad or expired secret) without echoing anything secret.
        raise SystemProblem(
            f"Microsoft refused the Graph sign-in: {result.get('error', 'unknown_error')}"
        )

    def _get(self, url: str, params: dict | None = None) -> dict:
        """GET a Graph URL and return the JSON, retrying throttling and outages.

        Raises:
            SystemProblem: On a refused token, a missing permission, an
                unknown mailbox, or when retries run out.
        """
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            try:
                response = self._session.get(url, headers=headers, params=params, timeout=30)
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt == MAX_RETRIES - 1:
                    raise SystemProblem(
                        f"Could not reach Graph: {type(error).__name__}"
                    ) from error
                self._wait(None, attempt, type(error).__name__)
                continue

            if response.ok:
                return response.json()
            if response.status_code == 401:
                raise SystemProblem("Graph rejected the sign-in token (401).")
            if response.status_code == 403:
                raise SystemProblem(
                    "Graph refused with 403. The app is probably missing Mail.Read, "
                    "or IT's mailbox scope on the app does not cover graph.mailbox."
                )
            if response.status_code == 404:
                raise SystemProblem(
                    "Graph returned 404. Check graph.mailbox in config.yaml."
                )
            if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                self._wait(response, attempt, str(response.status_code))
                continue
            raise SystemProblem(f"Graph request failed with {response.status_code}.")
        raise SystemProblem(f"Graph still failing after {MAX_RETRIES} attempts.")

    def _wait(self, response: requests.Response | None, attempt: int, reason: str) -> None:
        """Sleep before a retry, honoring Retry-After when Microsoft sends one."""
        seconds = 2**attempt
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after and retry_after.isdigit():
            seconds = int(retry_after)
        seconds = min(seconds, MAX_BACKOFF_SECONDS)
        self._log.warning("Graph %s, waiting %ss (attempt %d of %d)",
                          reason, seconds, attempt + 1, MAX_RETRIES)
        time.sleep(seconds)

    def _messages_on(self, day: date, sender: str) -> list[dict]:
        """List Inbox messages from sender received on day, local time, newest first.

        Raises:
            SystemProblem: On a Graph failure or if paging runs past MAX_PAGES.
        """
        start = datetime.combine(day, clock.min).astimezone().astimezone(timezone.utc)
        end = start + timedelta(days=1)
        # Graph string literals escape a quote by doubling it.
        literal = sender.strip().lower().replace("'", "''")
        url: str | None = f"{self._base}/mailFolders/{FOLDER}/messages"
        params: dict | None = {
            # receivedDateTime comes first in $filter because it is also the
            # $orderby property; Graph rejects the query otherwise.
            "$filter": (
                f"receivedDateTime ge {start:%Y-%m-%dT%H:%M:%SZ} and "
                f"receivedDateTime lt {end:%Y-%m-%dT%H:%M:%SZ} and "
                f"from/emailAddress/address eq '{literal}'"
            ),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,receivedDateTime,hasAttachments",
            "$top": PAGE_SIZE,
        }
        messages: list[dict] = []
        for _ in range(MAX_PAGES):
            payload = self._get(url, params)
            messages.extend(payload.get("value", []))
            # nextLink already carries every query option.
            url, params = payload.get("@odata.nextLink"), None
            if not url:
                return messages
        raise SystemProblem(f"More than {MAX_PAGES} pages of mail from the sender in one day.")

    def fetch_booking_sheet(self, day: date, sender: str, subject_phrase: str) -> bytes:
        """Return the .xlsx attached to the day's booking email.

        The email must come from sender, be received on day (local time), and
        have subject_phrase in its subject, ignoring case. Exactly one such
        email, with exactly one .xlsx attached, is required. Zero or more than
        one of either stops the run instead of picking one, because the wrong
        sheet means searching the wrong people with no sign anything is off.

        Raises:
            SystemProblem: If the email or attachment is missing or ambiguous,
                or Graph fails.
        """
        phrase = subject_phrase.casefold()
        matches = [
            message for message in self._messages_on(day, sender)
            if phrase in (message.get("subject") or "").casefold()
        ]
        if len(matches) != 1:
            raise SystemProblem(
                f"Expected one booking email on {day:%m/%d/%Y}, found {len(matches)}. "
                "Check the Inbox, or graph.sender and graph.subject_contains."
            )

        payload = self._get(f"{self._base}/messages/{matches[0]['id']}/attachments")
        sheets = [
            attachment for attachment in payload.get("value", [])
            if attachment.get("@odata.type") == "#microsoft.graph.fileAttachment"
            and not attachment.get("isInline")
            and (attachment.get("name") or "").lower().endswith(".xlsx")
        ]
        if len(sheets) != 1:
            raise SystemProblem(
                f"Expected one .xlsx on the booking email, found {len(sheets)}."
            )
        content = sheets[0].get("contentBytes")
        if not content:
            raise SystemProblem("Graph returned the .xlsx with no content.")
        self._log.info("Booking email found, spreadsheet downloaded")
        return base64.b64decode(content)
