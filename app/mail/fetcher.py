"""
שליפת מיילים מחשבון ה-Gmail הייעודי דרך IMAP.

אין כאן שום נגיעה בתשתית ארגונית — רק חשבון Gmail ו-App Password.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

from app import config
from app.ingest import synthetic_message_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchedEmail:
    message_id: str
    date: datetime
    subject: str
    body: str


class _HTMLToText(HTMLParser):
    """המרת HTML לטקסט — כשאין חלק text/plain במייל."""

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
    מעדיף text/plain. אם אין — ממיר את ה-HTML לטקסט.
    מדלג על קבצים מצורפים.
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
    except Exception:  # כותרת פגומה לא אמורה להפיל שליפה שלמה
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
            log.warning("תאריך לא תקין בכותרת המייל: %r", raw)
    return datetime.now(timezone.utc)


def fetch_unseen(mark_seen: bool = True, limit: int = 100) -> list[FetchedEmail]:
    """
    מושך מיילים שלא נקראו. מסמן כנקראים רק אחרי קריאה מוצלחת.

    הדדופליקציה האמיתית היא לפי Message-ID במסד, ולכן גם אם דגל ה-Seen
    יאבד — המלאי לא ייספר פעמיים.
    """
    if not config.imap_configured():
        raise RuntimeError("חסרים פרטי חיבור לתיבה (IMAP_USER / IMAP_PASSWORD).")

    results: list[FetchedEmail] = []
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    try:
        conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
        conn.select(config.IMAP_FOLDER)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"חיפוש בתיבה נכשל: {status}")

        uids = data[0].split()[:limit]
        for uid in uids:
            # BODY.PEEK כדי שהקריאה עצמה לא תסמן כנקרא — אנחנו מחליטים מתי.
            status, payload = conn.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("לא ניתן לקרוא את המייל %r", uid)
                continue

            msg = email.message_from_bytes(payload[0][1])
            body = extract_body(msg)
            message_id = _decode_header_value(msg.get("Message-ID")).strip()
            if not message_id:
                # מייל בלי Message-ID — נגזור מזהה יציב מהתוכן, כדי שהדדופליקציה
                # תמשיך לעבוד גם עליו.
                message_id = synthetic_message_id(body)

            results.append(
                FetchedEmail(
                    message_id=message_id,
                    date=_message_date(msg),
                    subject=_decode_header_value(msg.get("Subject")),
                    body=body,
                )
            )
            if mark_seen:
                conn.store(uid, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()

    return results
