"""
הגדרה ראשונית של האתר — סיסמת כניסה, מפתח חתימה, וחיבור לתיבת הדואר.

    py -m app.setup

הכלי כותב את הערכים לקובץ .env שיושב לצד הקוד. הקובץ הזה לא נכנס ל-git
ולא נשלח לשום מקום — הוא נשאר על המחשב שלך בלבד.
"""
from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

from app import config, env_file
from app.auth import hash_password

MIN_PASSWORD_LENGTH = 8


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "כן/לא" if default else "כן/לא"
    while True:
        answer = input(f"{prompt} ({hint}) [{'כן' if default else 'לא'}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"כן", "y", "yes", "כ"}:
            return True
        if answer in {"לא", "n", "no", "ל"}:
            return False
        print("  לא הבנתי. אפשר לכתוב 'כן' או 'לא'.")


def _ask_password() -> str:
    while True:
        # getpass לא מציג את התווים בזמן ההקלדה.
        password = getpass.getpass("סיסמה חדשה לאתר: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"  קצרה מדי — לפחות {MIN_PASSWORD_LENGTH} תווים.")
            continue
        if password != getpass.getpass("שוב, לאימות: "):
            print("  הסיסמאות אינן תואמות. ננסה שוב.")
            continue
        return password


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """
    מעדכן מפתחות קיימים במקומם ומוסיף חדשים בסוף.
    הערות ושורות שלא נגענו בהן נשמרות כפי שהן.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    remaining = dict(updates)
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    env_path = config.ENV_PATH
    existing = env_file.parse(env_path.read_text(encoding="utf-8-sig")) if env_path.is_file() else {}
    updates: dict[str, str] = {}

    print("\n=== הגדרת האתר — מרכז ציוד שפלה ===\n")
    print(f"ההגדרות ייכתבו לקובץ: {env_path}\n")

    # --- 1. סיסמת כניסה ---
    print("--- 1. סיסמת כניסה לאתר ---")
    if existing.get("APP_PASSWORD_HASH"):
        print("כבר מוגדרת סיסמה.")
        if not _ask_yes_no("להחליף אותה?", default=False):
            print("  הסיסמה הקיימת נשמרת.")
        else:
            updates["APP_PASSWORD_HASH"] = hash_password(_ask_password())
            print("  ✓ הסיסמה עודכנה.")
    else:
        print("זו הסיסמה שתידרש בכל כניסה לאתר.")
        updates["APP_PASSWORD_HASH"] = hash_password(_ask_password())
        print("  ✓ נקבעה סיסמה.")

    # --- 2. מפתח חתימה ---
    print("\n--- 2. מפתח חתימה לעוגיות ---")
    if existing.get("SESSION_SECRET"):
        print("  ✓ כבר קיים, לא נוגעים בו (החלפה תנתק אותך מהאתר).")
    else:
        updates["SESSION_SECRET"] = secrets.token_urlsafe(48)
        print("  ✓ נוצר אוטומטית מחרוזת אקראית. אין מה לזכור אותה.")

    # --- 3. תיבת הדואר ---
    print("\n--- 3. חיבור לתיבת ה-Gmail הייעודית ---")
    print("אפשר לדלג ולהגדיר בהמשך; עד אז אפשר להדביק מיילים ידנית באתר.")
    if _ask_yes_no("להגדיר עכשיו?", default=False):
        user = _ask("כתובת ה-Gmail", existing.get("IMAP_USER", ""))
        print("\nצריך App Password של 16 תווים — לא סיסמת החשבון הרגילה.")
        print("איך משיגים: myaccount.google.com/apppasswords (דורש אימות דו-שלבי פעיל).")
        app_password = getpass.getpass("App Password: ").replace(" ", "")
        if user and app_password:
            if len(app_password) != 16:
                print(f"  שימי לב: הודבקו {len(app_password)} תווים ולא 16. אם החיבור ייכשל, זו הסיבה.")
            updates["IMAP_USER"] = user
            updates["IMAP_PASSWORD"] = app_password
            print("  ✓ פרטי התיבה נשמרו.")
        else:
            print("  דילגנו — לא הוזנו פרטים מלאים.")
    else:
        print("  דילגנו.")

    # --- 4. ענן ---
    print("\n--- 4. איפה האתר ירוץ ---")
    print("COOKIE_SECURE שולח את עוגיית הכניסה רק על חיבור מוצפן (HTTPS).")
    print("בהרצה מקומית (http://localhost) הוא חייב להישאר כבוי, אחרת לא תוכלי להיכנס.")
    if _ask_yes_no("האתר ירוץ בענן מאחורי HTTPS?", default=False):
        updates["COOKIE_SECURE"] = "1"
        print("  ✓ הודלק.")
    else:
        updates["COOKIE_SECURE"] = "0"
        print("  ✓ כבוי — מתאים להרצה מקומית.")

    _write_env(env_path, updates)

    print("\n" + "=" * 50)
    print("ההגדרות נשמרו. עכשיו להריץ:\n")
    print("    py -m app.server        (ווינדוס)")
    print("    python3 -m app.server   (מק/לינוקס)\n")
    print("הקובץ .env נשאר על המחשב שלך בלבד ולא נכנס ל-git.")
    print("=" * 50 + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nבוטל. לא נשמרו שינויים.")
        sys.exit(1)
