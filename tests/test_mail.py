"""חילוץ גוף המייל — קידוד עברית, HTML, תאריכים, ושליפה מהתיבה."""
from __future__ import annotations

import email
import unittest
from email.message import EmailMessage
from unittest import mock

from tests.base import SAMPLE_EMAIL

from app import config
from app.mail import fetcher
from app.mail.fetcher import _message_date, extract_body, fetch_unseen, html_to_text
from app.parsing.issuance_parser import parse


def _build(body: str, subtype: str = "plain", charset: str = "utf-8") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "הנפקת ציוד"
    msg["Date"] = "Tue, 01 Sep 2026 14:32:00 +0300"
    msg.set_content(body, subtype=subtype, charset=charset)
    return msg


class BodyExtraction(unittest.TestCase):
    def test_plain_utf8(self) -> None:
        extracted = extract_body(_build(SAMPLE_EMAIL))
        self.assertIn("המוצרים שהונפקו:", extracted)
        self.assertEqual(parse(extracted).errors, [])

    def test_hebrew_survives_quoted_printable_roundtrip(self) -> None:
        raw = _build(SAMPLE_EMAIL).as_bytes()
        parsed = parse(extract_body(email.message_from_bytes(raw)))
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.issuer, "יואב חמאם")
        self.assertEqual(len(parsed.lines), 7)

    def test_plain_is_preferred_over_html(self) -> None:
        msg = EmailMessage()
        msg.set_content(SAMPLE_EMAIL)
        msg.add_alternative("<p>גרסת HTML שאין להשתמש בה</p>", subtype="html")
        self.assertNotIn("שאין להשתמש", extract_body(msg))
        self.assertIn("המוצרים שהונפקו:", extract_body(msg))

    def test_html_only_email_is_converted_and_parses(self) -> None:
        html = "".join(f"<p>{line}</p>" for line in SAMPLE_EMAIL.split("\n") if line.strip())
        msg = EmailMessage()
        msg.set_content(html, subtype="html")
        parsed = parse(extract_body(msg))
        self.assertEqual(parsed.errors, [])
        self.assertEqual(len(parsed.lines), 7)

    def test_attachments_are_skipped(self) -> None:
        msg = EmailMessage()
        msg.set_content(SAMPLE_EMAIL)
        msg.add_attachment(b"\x00binary", maintype="application", subtype="octet-stream", filename="x.bin")
        self.assertEqual(parse(extract_body(msg)).errors, [])


class HtmlConversion(unittest.TestCase):
    def test_block_tags_become_line_breaks(self) -> None:
        self.assertEqual(html_to_text("<p>שלום</p><p>עולם</p>").split("\n")[0], "שלום")

    def test_scripts_and_styles_are_dropped(self) -> None:
        text = html_to_text("<style>p{color:red}</style><script>alert(1)</script><p>תוכן</p>")
        self.assertEqual(text.strip(), "תוכן")

    def test_entities_are_decoded(self) -> None:
        self.assertIn('פד גזה 10*10', html_to_text("<p>פד גזה 10*10</p>"))


class FakeIMAP:
    """תיבה מזויפת — מאפשרת לבדוק את fetch_unseen בלי שרת אמיתי."""

    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.marked_seen: list[bytes] = []
        self.logged_in = False
        self.closed = False

    def __call__(self, host, port):  # מחליף את imaplib.IMAP4_SSL
        return self

    def login(self, user, password):
        self.logged_in = True
        return ("OK", [b""])

    def select(self, folder):
        return ("OK", [b"1"])

    def search(self, charset, criterion):
        assert criterion == "UNSEEN"
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))])

    def fetch(self, uid, spec):
        assert b"PEEK" in spec.encode(), "חובה לקרוא ב-PEEK כדי לא לסמן כנקרא מוקדם מדי"
        return ("OK", [(b"1 (BODY[] {n}", self.messages[int(uid) - 1]), b")"])

    def store(self, uid, flags, value):
        self.marked_seen.append(uid)
        return ("OK", [b""])

    def close(self):
        self.closed = True

    def logout(self):
        return ("BYE", [b""])


class FetchUnseen(unittest.TestCase):
    def setUp(self) -> None:
        self._user, self._password = config.IMAP_USER, config.IMAP_PASSWORD
        config.IMAP_USER, config.IMAP_PASSWORD = "box@gmail.com", "app-password"

    def tearDown(self) -> None:
        config.IMAP_USER, config.IMAP_PASSWORD = self._user, self._password

    @staticmethod
    def _message(message_id: str | None) -> bytes:
        msg = EmailMessage()
        msg["Subject"] = "הנפקת ציוד"
        msg["Date"] = "Tue, 01 Sep 2026 14:32:00 +0300"
        if message_id:
            msg["Message-ID"] = message_id
        msg.set_content(SAMPLE_EMAIL)
        return msg.as_bytes()

    def test_uses_the_message_id_header(self) -> None:
        fake = FakeIMAP([self._message("<abc@mail.example>")])
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", fake):
            emails = fetch_unseen()
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0].message_id, "<abc@mail.example>")
        self.assertEqual(parse(emails[0].body).errors, [])

    def test_email_without_message_id_gets_a_content_derived_id(self) -> None:
        """
        הנתיב הזה הסתיר באג: הוא ייבא ממודול שכבר לא קיים.
        מייל בלי Message-ID עדיין חייב לקבל מזהה יציב, אחרת הדדופליקציה נשברת.
        """
        fake = FakeIMAP([self._message(None)])
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", fake):
            emails = fetch_unseen()
        self.assertEqual(len(emails), 1)
        self.assertTrue(emails[0].message_id.startswith("paste-"))
        self.assertEqual(parse(emails[0].body).errors, [])

    def test_same_body_yields_the_same_id_twice(self) -> None:
        fake = FakeIMAP([self._message(None), self._message(None)])
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", fake):
            emails = fetch_unseen()
        self.assertEqual(emails[0].message_id, emails[1].message_id)

    def test_messages_are_marked_seen_and_connection_closed(self) -> None:
        fake = FakeIMAP([self._message("<a@x>"), self._message("<b@x>")])
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", fake):
            fetch_unseen()
        self.assertEqual(fake.marked_seen, [b"1", b"2"])
        self.assertTrue(fake.logged_in)
        self.assertTrue(fake.closed)

    def test_mark_seen_can_be_disabled(self) -> None:
        fake = FakeIMAP([self._message("<a@x>")])
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", fake):
            fetch_unseen(mark_seen=False)
        self.assertEqual(fake.marked_seen, [])

    def test_missing_credentials_raise_a_clear_error(self) -> None:
        config.IMAP_USER = ""
        with self.assertRaises(RuntimeError) as ctx:
            fetch_unseen()
        self.assertIn("חסרים פרטי חיבור", str(ctx.exception))


class SyncHandlesFailures(unittest.TestCase):
    def test_connection_error_is_reported_not_raised(self) -> None:
        """תקלת רשת או אימות לא אמורה להפיל את השרת."""
        from app import mail_sync

        with mock.patch.object(fetcher, "fetch_unseen", side_effect=OSError("אין חיבור")):
            result = mail_sync.sync_once()
        self.assertIsNotNone(result.error)
        self.assertIn("שגיאה במשיכת מיילים", result.summary())


class DateHandling(unittest.TestCase):
    def test_reads_the_date_header(self) -> None:
        parsed = _message_date(_build("x"))
        self.assertEqual((parsed.year, parsed.month, parsed.day, parsed.hour), (2026, 9, 1, 14))

    def test_invalid_date_falls_back_to_now(self) -> None:
        msg = EmailMessage()
        msg["Date"] = "לא תאריך"
        self.assertIsNotNone(_message_date(msg))

    def test_missing_date_falls_back_to_now(self) -> None:
        self.assertIsNotNone(_message_date(EmailMessage()))


if __name__ == "__main__":
    unittest.main()
