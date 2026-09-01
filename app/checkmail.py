"""
בדיקת החיבור לתיבת הדואר.

    py -m app.checkmail

מתחבר, מדווח מה נמצא, ולא מסמן שום מייל כנקרא ולא נוגע במלאי.
נועד לענות על השאלה "האם ההגדרות נכונות?" בלי להמתין למחזור המשיכה.
"""
from __future__ import annotations

import imaplib
import socket
import ssl
import sys

from app import config
from app.mail import fetcher
from app.parsing import issuance_parser


def _explain(error: Exception) -> str:
    """מתרגם שגיאות רשת ואימות להסבר שאפשר לפעול לפיו."""
    text = str(error)
    if isinstance(error, imaplib.IMAP4.error):
        lowered = text.lower()
        if "invalid credentials" in lowered or "authentication failed" in lowered:
            return (
                "האימות נדחה. הסיבות השכיחות:\n"
                "  • הוזנה סיסמת החשבון הרגילה ולא App Password בן 16 תווים.\n"
                "  • ה-App Password בוטל או נוצר לחשבון אחר.\n"
                "  • יש טעות בכתובת המייל.\n"
                "  להגדרה מחדש:  py -m app.setup"
            )
        return f"השרת דחה את הבקשה: {text}"
    if isinstance(error, socket.gaierror):
        return f"לא נמצאה הכתובת {config.IMAP_HOST} — כנראה אין חיבור לאינטרנט או שהשם שגוי."
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "החיבור נתקע. ייתכן שחומת אש חוסמת יציאה בפורט 993."
    if isinstance(error, ssl.SSLError):
        return f"תקלת הצפנה בחיבור: {text}"
    if isinstance(error, OSError):
        return f"תקלת רשת: {text}"
    return text


def main() -> int:
    print("\n=== בדיקת חיבור לתיבת הדואר ===\n")

    if not config.imap_configured():
        print("לא הוגדרו פרטי תיבה.")
        print("  להגדרה:  py -m app.setup")
        print("\nעד אז אפשר להדביק מיילים ידנית במסך 'הדבקת מייל' באתר.\n")
        return 1

    print(f"  שרת:    {config.IMAP_HOST}:{config.IMAP_PORT}")
    print(f"  חשבון:  {config.IMAP_USER}")
    print(f"  תיקייה: {config.IMAP_FOLDER}")
    print(f"  סיסמה:  הוגדרה ({len(config.IMAP_PASSWORD)} תווים)")
    if len(config.IMAP_PASSWORD) != 16:
        print("          שימי לב: App Password של גוגל הוא בן 16 תווים.")
    print("\nמתחבר...\n")

    try:
        # בלי is_known — הבדיקה מציגה את כל מה שבחלון הזמן, גם מה שכבר נקלט.
        emails = fetcher.fetch_recent()
    except Exception as exc:  # ההסבר חשוב יותר מ-traceback
        print("✗ החיבור נכשל.\n")
        print(_explain(exc))
        print()
        return 1

    print("✓ החיבור הצליח.\n")

    if not emails:
        print(f"אין מיילים ב-{config.LOOKBACK_DAYS} הימים האחרונים בתיבה.")
        print("  אם ציפית למיילים — ודאי שמיילי ההנפקה באמת מגיעים לחשבון הזה,")
        print("  ושהם נשלחו בתקופה הזו.\n")
        return 0

    print(f"נמצאו {len(emails)} מיילים ב-{config.LOOKBACK_DAYS} הימים האחרונים:\n")
    recognised = 0
    for item in emails[:20]:
        parsed = issuance_parser.parse(item.body)
        if parsed.ok and issuance_parser.center_matches(parsed):
            recognised += 1
            mark, detail = "✓", f"{len(parsed.lines)} פריטים · מנפיק: {parsed.issuer or '—'}"
        elif parsed.ok:
            mark, detail = "•", f'מרכז אחר: {parsed.center or "לא ידוע"} — לא ייספר'
        else:
            mark, detail = "✗", parsed.errors[0]
        print(f"  {mark} {item.subject or '(ללא נושא)'}")
        print(f"      {detail}")

    print(f"\nמתוכם {recognised} מזוהים כהנפקות של {issuance_parser.load_format().expected_center}.")
    print("\nשום דבר לא נכנס למלאי ומצב התיבה לא השתנה — זו בדיקה בלבד.")
    print("לקליטה בפועל: להריץ את השרת וללחוץ '⟳ משוך מיילים'.\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nבוטל.")
        sys.exit(1)
