"""חילוץ גוף המייל — קידוד עברית, HTML, ותאריכים."""
from __future__ import annotations

import email
import unittest
from email.message import EmailMessage

from tests.base import SAMPLE_EMAIL

from app.mail.fetcher import _message_date, extract_body, html_to_text
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
