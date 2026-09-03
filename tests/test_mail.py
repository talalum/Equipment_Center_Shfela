"""Extracting the email body — Hebrew encoding, HTML, dates, and fetching from the mailbox."""
from __future__ import annotations

import email
import re
import unittest
from email.message import EmailMessage
from unittest import mock

from tests.base import SAMPLE_EMAIL

from app import config
from app.mail import fetcher
from app.mail.fetcher import _message_date, extract_body, fetch_recent, html_to_text
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
    """A fake mailbox — lets the fetch be tested without a real server."""

    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.marked_seen: list[bytes] = []
        self.logged_in = False
        self.closed = False
        self.searches: list[tuple] = []
        self.header_fetches = 0
        self.body_fetches = 0

    def __call__(self, host, port):  # stands in for imaplib.IMAP4_SSL
        return self

    def login(self, user, password):
        self.logged_in = True
        return ("OK", [b""])

    def select(self, folder):
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        self.searches.append(criteria)
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))])

    def fetch(self, uid, spec):
        assert "PEEK" in spec, "must read with PEEK so the mailbox state is not changed"
        raw = self.messages[int(uid) - 1]
        if "HEADER.FIELDS" in spec:
            self.header_fetches += 1
            header = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
            match = re.search(rb"(?im)^message-id:.*$", header)
            return ("OK", [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {n}", (match.group(0) if match else b"")), b")"])
        self.body_fetches += 1
        return ("OK", [(b"1 (BODY[] {n}", raw), b")"])

    def store(self, uid, flags, value):
        self.marked_seen.append(uid)
        return ("OK", [b""])

    def close(self):
        self.closed = True

    def logout(self):
        return ("BYE", [b""])


class FetchRecent(unittest.TestCase):
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

    def _fetch(self, fake, **kwargs):
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", fake):
            return fetch_recent(**kwargs)

    def test_searches_by_date_not_by_unread_flag(self) -> None:
        """
        The central regression: searching by UNSEEN means an email someone has
        already opened in the mailbox would never be collected — an issuance
        vanishing silently.
        """
        fake = FakeIMAP([self._message("<a@x>")])
        self._fetch(fake)
        self.assertEqual(len(fake.searches), 1)
        self.assertEqual(fake.searches[0][0], "SINCE")
        self.assertNotIn("UNSEEN", fake.searches[0])

    def test_a_message_already_read_is_still_fetched(self) -> None:
        fake = FakeIMAP([self._message("<already-read@x>")])
        fake.marked_seen.append(b"1")  # as though someone had opened it in the mailbox
        emails = self._fetch(fake)
        self.assertEqual(len(emails), 1)

    def test_nothing_is_marked_as_read(self) -> None:
        """Reading the mailbox does not change its state — deduplication rests on the database."""
        fake = FakeIMAP([self._message("<a@x>"), self._message("<b@x>")])
        self._fetch(fake)
        self.assertEqual(fake.marked_seen, [])
        self.assertTrue(fake.logged_in)
        self.assertTrue(fake.closed)

    def test_uses_the_message_id_header(self) -> None:
        fake = FakeIMAP([self._message("<abc@mail.example>")])
        emails = self._fetch(fake)
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0].message_id, "<abc@mail.example>")
        self.assertEqual(parse(emails[0].body).errors, [])

    def test_known_messages_skip_the_body_fetch(self) -> None:
        """A repeat scan must be cheap: headers only for emails already taken in."""
        fake = FakeIMAP([self._message("<known@x>"), self._message("<fresh@x>")])
        emails = self._fetch(fake, is_known=lambda mid: mid == "<known@x>")
        self.assertEqual([e.message_id for e in emails], ["<fresh@x>"])
        self.assertEqual(fake.header_fetches, 2)
        self.assertEqual(fake.body_fetches, 1, "a body is fetched only for the new email")

    def test_everything_is_new_when_no_filter_is_given(self) -> None:
        fake = FakeIMAP([self._message("<a@x>"), self._message("<b@x>")])
        self.assertEqual(len(self._fetch(fake)), 2)

    def test_email_without_message_id_gets_a_content_derived_id(self) -> None:
        fake = FakeIMAP([self._message(None)])
        emails = self._fetch(fake)
        self.assertEqual(len(emails), 1)
        self.assertTrue(emails[0].message_id.startswith("paste-"))
        self.assertEqual(parse(emails[0].body).errors, [])

    def test_same_body_yields_the_same_id_twice(self) -> None:
        fake = FakeIMAP([self._message(None), self._message(None)])
        emails = self._fetch(fake)
        self.assertEqual(emails[0].message_id, emails[1].message_id)

    def test_id_less_message_can_also_be_skipped_as_known(self) -> None:
        fake = FakeIMAP([self._message(None)])
        seen = self._fetch(fake)[0].message_id
        fake2 = FakeIMAP([self._message(None)])
        self.assertEqual(self._fetch(fake2, is_known=lambda mid: mid == seen), [])

    def test_results_are_chronological(self) -> None:
        fake = FakeIMAP([self._message("<first@x>"), self._message("<second@x>")])
        self.assertEqual([e.message_id for e in self._fetch(fake)], ["<first@x>", "<second@x>"])

    def test_missing_credentials_raise_a_clear_error(self) -> None:
        config.IMAP_USER = ""
        with self.assertRaises(RuntimeError) as ctx:
            fetch_recent()
        self.assertIn("חסרים פרטי חיבור", str(ctx.exception))


class SyncHandlesFailures(unittest.TestCase):
    def test_connection_error_is_reported_not_raised(self) -> None:
        """A network or authentication failure must not bring the server down."""
        from app import mail_sync

        with mock.patch.object(fetcher, "fetch_recent", side_effect=OSError("no connection")):
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
