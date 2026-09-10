"""System settings. Every secret comes from an environment variable, never from the code."""
from __future__ import annotations

import os
from pathlib import Path

from app import env_file

BASE_DIR = Path(__file__).resolve().parent.parent

# Loaded before the settings are read. A variable already present in the
# environment wins over the file, so hosting-service settings take precedence
# in the cloud.
ENV_PATH = Path(os.environ.get("ENV_FILE", BASE_DIR / ".env"))
env_file.load(ENV_PATH)

# --- Database ---
# Two engines are supported, and the choice between them is made by
# DATABASE_URL alone:
#
#   DATABASE_URL set    → PostgreSQL. The data lives on a separate server, so
#                         the container is stateless and can be hosted without
#                         a persistent disk.
#   DATABASE_URL empty  → SQLite. A local file, exactly as when running locally.
#
# The schema and the queries are identical in both modes — see app/db.py.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Path to the SQLite file. Relevant only when there is no DATABASE_URL. In the
# cloud, if SQLite is used anyway, it must sit on a persistent disk or the data
# will be wiped.
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "warehouse.db"))

# --- Email format ---
EMAIL_FORMAT_PATH = Path(os.environ.get("EMAIL_FORMAT_PATH", BASE_DIR / "config" / "email_format.yaml"))

# --- Initial import file ---
DEFAULT_IMPORT_CSV = Path(os.environ.get("DEFAULT_IMPORT_CSV", BASE_DIR / "data" / "Inventory_Report.csv"))

# --- IMAP (the dedicated Gmail account) ---
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")  # Gmail App Password
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "5"))
# How many days back each fetch scans. Large enough to catch up on a period in
# which the system was down; double counting is prevented by Message-ID anyway.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))

# --- Security ---
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", "")
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "300"))
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(14 * 24 * 3600)))
# Turn on in the cloud (behind HTTPS) — stops the cookie from being sent over
# an unencrypted connection.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"}

# --- Server ---
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# --- General ---
TZ_NAME = os.environ.get("TZ_NAME", "Asia/Jerusalem")

# --- File upload (the standard-file import in /items) ---
# Both limits are enforced on the server: web.py answers an over-sized request
# body with 413, and main.py rejects a file whose suffix is not listed here. The
# accept attribute of the form field is built from the same table, but it is
# nothing more than a hint to the browser and cannot be relied upon.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
#: Permitted suffix -> the MIME type the browser reports for it. The old binary
#: .xls is deliberately absent: it is a different format altogether and cannot
#: be read without an external library.
ALLOWED_UPLOAD_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
}


def imap_configured() -> bool:
    """Whether there are enough details to attempt a mailbox connection."""
    return bool(IMAP_USER and IMAP_PASSWORD)


def upload_accept() -> str:
    """The accept attribute of the file field, both suffixes and MIME types."""
    return ",".join(part for suffix, mime in ALLOWED_UPLOAD_TYPES.items() for part in (suffix, mime))


def upload_types_label() -> str:
    """The permitted types for the interface and for error messages, e.g. 'CSV'."""
    return ", ".join(suffix.lstrip(".").upper() for suffix in ALLOWED_UPLOAD_TYPES)


def upload_size_label() -> str:
    """The size limit for the interface and for error messages, e.g. '8MB'."""
    if MAX_UPLOAD_BYTES >= 1024 * 1024:
        return f"{MAX_UPLOAD_BYTES / (1024 * 1024):g}MB"
    return f"{MAX_UPLOAD_BYTES / 1024:g}KB"


def upload_suffix_allowed(filename: str) -> bool:
    """Whether the uploaded file name carries a permitted suffix."""
    return filename.strip().lower().endswith(tuple(ALLOWED_UPLOAD_TYPES))
