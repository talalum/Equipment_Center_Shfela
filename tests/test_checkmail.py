"""כלי בדיקת החיבור לתיבה."""
from __future__ import annotations

import imaplib
import io
import socket
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from unittest import mock

from tests.base import SAMPLE_EMAIL
from tests.test_mail import FakeIMAP

from app import checkmail, config
from app.mail import fetcher


def _message(subject: str, body: str, message_id: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Date"] = "Tue, 01 Sep 2026 14:32:00 +0300"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


class CheckMail(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (config.IMAP_USER, config.IMAP_PASSWORD)
        config.IMAP_USER, config.IMAP_PASSWORD = "box@gmail.com", "abcdefghijklmnop"

    def tearDown(self) -> None:
        config.IMAP_USER, config.IMAP_PASSWORD = self._saved

    @staticmethod
    def _run(fake_or_error) -> tuple[int, str]:
        buffer = io.StringIO()
        target = (
            {"side_effect": fake_or_error}
            if isinstance(fake_or_error, Exception)
            else {"new": fake_or_error}
        )
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", **target):
            with redirect_stdout(buffer):
                code = checkmail.main()
        return code, buffer.getvalue()

    def test_reports_missing_configuration(self) -> None:
        config.IMAP_USER = ""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = checkmail.main()
        self.assertEqual(code, 1)
        self.assertIn("לא הוגדרו פרטי תיבה", buffer.getvalue())

    def test_never_prints_the_password(self) -> None:
        config.IMAP_PASSWORD = "supersecret12345"
        _, output = self._run(FakeIMAP([]))
        self.assertNotIn("supersecret12345", output)
        self.assertIn("16 תווים", output)

    def test_warns_when_password_is_not_16_characters(self) -> None:
        config.IMAP_PASSWORD = "short"
        _, output = self._run(FakeIMAP([]))
        self.assertIn("בן 16 תווים", output)

    def test_empty_mailbox_is_a_success(self) -> None:
        code, output = self._run(FakeIMAP([]))
        self.assertEqual(code, 0)
        self.assertIn("החיבור הצליח", output)
        self.assertIn("אין מיילים שלא נקראו", output)

    def test_classifies_each_kind_of_message(self) -> None:
        code, output = self._run(
            FakeIMAP(
                [
                    _message("הנפקה", SAMPLE_EMAIL, "<a@x>"),
                    _message("אחר", SAMPLE_EMAIL.replace("מרכז ציוד שפלה", "מרכז ציוד ירושלים"), "<b@x>"),
                    _message("ניוזלטר", "טקסט שיווקי", "<c@x>"),
                ]
            )
        )
        self.assertEqual(code, 0)
        self.assertIn("7 פריטים", output)
        self.assertIn("מרכז אחר", output)
        self.assertIn("לא נמצא העוגן", output)
        self.assertIn("מתוכם 1", output)

    def test_does_not_mark_anything_as_read(self) -> None:
        """בדיקה חייבת להיות חסרת תופעות לוואי — אחרת היא 'תבלע' מיילים."""
        fake = FakeIMAP([_message("הנפקה", SAMPLE_EMAIL, "<a@x>")])
        self._run(fake)
        self.assertEqual(fake.marked_seen, [])

    def test_authentication_failure_explains_app_password(self) -> None:
        error = imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
        code, output = self._run(error)
        self.assertEqual(code, 1)
        self.assertIn("האימות נדחה", output)
        self.assertIn("App Password", output)

    def test_dns_failure_is_explained(self) -> None:
        code, output = self._run(socket.gaierror("Name or service not known"))
        self.assertEqual(code, 1)
        self.assertIn("אין חיבור לאינטרנט", output)

    def test_timeout_mentions_the_firewall(self) -> None:
        code, output = self._run(TimeoutError("timed out"))
        self.assertEqual(code, 1)
        self.assertIn("993", output)

    def test_unexpected_error_does_not_crash(self) -> None:
        code, output = self._run(RuntimeError("משהו מוזר"))
        self.assertEqual(code, 1)
        self.assertIn("נכשל", output)


if __name__ == "__main__":
    unittest.main()
