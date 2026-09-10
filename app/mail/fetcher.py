"""
Fetching emails from the dedicated Gmail account over IMAP.

Nothing here touches any corporate infrastructure — only a Gmail account and
an App Password.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

from app import config, localtime
from app.ingest import synthetic_message_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchedEmail:
    message_id: str
    date: datetime
    subject: str
    body: str


class _HTMLToText(HTMLParser):
    """HTML to text conversion — for when the email has no text/plain part."""

    _BLOCK_TAGS = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        joined = re.sub(r"[ \t ]+", " ", joined)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(unescape(html))
    parser.close()
    return parser.text()


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_body(msg: Message) -> str:
    """
    Prefers text/plain. Failing that, converts the HTML to text.
    Attachments are skipped.
    """
    plain: list[str] = []
    html: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            if part.get_content_type() == "text/plain":
                plain.append(_decode_part(part))
            elif part.get_content_type() == "text/html":
                html.append(_decode_part(part))
    else:
        content = _decode_part(msg)
        (html if msg.get_content_type() == "text/html" else plain).append(content)

    if any(p.strip() for p in plain):
        return "\n".join(plain).strip()
    if html:
        return html_to_text("\n".join(html))
    return ""


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # a malformed header must not bring down a whole fetch
        return value


def _message_date(msg: Message) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError):
            log.warning("Invalid date in the email header: %r", raw)
    return datetime.now(timezone.utc)


_MESSAGE_ID_RE = re.compile(rb"^message-id:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _header_message_id(raw: bytes) -> str:
    match = _MESSAGE_ID_RE.search(raw)
    return _decode_header_value(match.group(1).decode("utf-8", "replace")).strip() if match else ""


def fetch_recent(
    is_known: Callable[[str], bool] | None = None,
    days: int | None = None,
    limit: int = 500,
    since: datetime | None = None,
) -> list[FetchedEmail]:
    """
    Fetches emails from the recent period, independently of the "seen" flag.

    Why not UNSEEN: if someone opens the mailbox and glances at an issuance email
    while the system is down, the email is marked as read and would never be
    collected — an issuance vanishing silently. Instead a fixed time window is
    scanned, and the protection against double counting rests on the Message-ID
    already being in the database.

    is_known takes a Message-ID and returns whether it has already been taken in.
    Bodies are fetched only for new emails, which keeps a repeat scan cheap.

    `since` narrows the window further — the intake cutoff, so that emails which
    would be left out of the count anyway are not dragged over the network. It is
    a saving only: the binding decision is made on intake (app/ingest.py), never
    here.
    """
    if not config.imap_configured():
        raise RuntimeError("חסרים פרטי חיבור לתיבה (IMAP_USER / IMAP_PASSWORD).")

    lookback = days if days is not None else config.LOOKBACK_DAYS
    window_start = datetime.now(timezone.utc) - timedelta(days=lookback)
    if since is not None and localtime.as_utc(since) > window_start:
        window_start = localtime.as_utc(since)

    # The date is taken in local time, because a cutoff of "from the 10th" means
    # the 10th here — at 01:00 in Israel it is still the 9th in UTC.
    #
    # A day is then deliberately subtracted: IMAP compares whole dates in SINCE,
    # in the mailbox timezone, and the mailbox need not agree with ours. On the
    # exact boundary that would drop an email that really is inside the window —
    # so the scan is made one day too wide, and the precise filtering is left to
    # intake, where the full timestamp is available.
    since_date = (localtime.to_local(window_start) - timedelta(days=1)).strftime("%d-%b-%Y")

    results: list[FetchedEmail] = []
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    try:
        conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
        conn.select(config.IMAP_FOLDER)
        status, data = conn.search(None, "SINCE", since_date)
        if status != "OK":
            raise RuntimeError(f"חיפוש בתיבה נכשל: {status}")

        # Newest first, so that even a busy mailbox is caught up on what matters first.
        uids = list(reversed(data[0].split()))[:limit]
        for uid in uids:
            # BODY.PEEK only — our reading never changes the state of the mailbox.
            status, head = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if status != "OK" or not head or not isinstance(head[0], tuple):
                log.warning("Could not read the headers of email %r", uid)
                continue

            message_id = _header_message_id(head[0][1])
            if message_id and is_known and is_known(message_id):
                continue  # already taken in — no need to fetch the body

            status, payload = conn.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("Could not read email %r", uid)
                continue

            msg = email.message_from_bytes(payload[0][1])
            body = extract_body(msg)
            if not message_id:
                # An email with no Message-ID — a stable id derived from the content,
                # so that deduplication keeps working for it too.
                message_id = synthetic_message_id(body)
                if is_known and is_known(message_id):
                    continue

            results.append(
                FetchedEmail(
                    message_id=message_id,
                    date=_message_date(msg),
                    subject=_decode_header_value(msg.get("Subject")),
                    body=body,
                )
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()

    results.reverse()  # so intake happens in chronological order
    return results
