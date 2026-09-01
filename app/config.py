"""הגדרות המערכת. כל הסודות מגיעים ממשתני סביבה, לעולם לא מהקוד."""
from __future__ import annotations

import os
from pathlib import Path

from app import env_file

BASE_DIR = Path(__file__).resolve().parent.parent

# נטען לפני קריאת ההגדרות. משתנה שכבר קיים בסביבה מנצח את הקובץ, כדי
# שהגדרות של שירות האירוח יגברו בענן.
ENV_PATH = Path(os.environ.get("ENV_FILE", BASE_DIR / ".env"))
env_file.load(ENV_PATH)

# --- מסד נתונים ---
# קובץ SQLite. בענן חייב לשבת על דיסק קבוע (persistent volume), אחרת
# הנתונים יימחקו בכל דיפלוי מחדש — ראי README.
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "warehouse.db"))

# --- תבנית המייל ---
EMAIL_FORMAT_PATH = Path(os.environ.get("EMAIL_FORMAT_PATH", BASE_DIR / "config" / "email_format.yaml"))

# --- קובץ הייבוא הראשוני ---
DEFAULT_IMPORT_CSV = Path(os.environ.get("DEFAULT_IMPORT_CSV", BASE_DIR / "data" / "Inventory_Report.csv"))

# --- IMAP (חשבון ה-Gmail הייעודי) ---
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")  # App Password של Gmail
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "5"))

# --- אבטחה ---
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", "")
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "300"))
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(14 * 24 * 3600)))
# בענן (מאחורי HTTPS) יש להדליק — מונע שליחת העוגייה על חיבור לא מוצפן.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"}

# --- שרת ---
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# --- כללי ---
TZ_NAME = os.environ.get("TZ_NAME", "Asia/Jerusalem")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))


def imap_configured() -> bool:
    """האם יש די פרטים כדי לנסות להתחבר לתיבה."""
    return bool(IMAP_USER and IMAP_PASSWORD)
