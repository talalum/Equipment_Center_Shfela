"""
שכבת מסד הנתונים — SQLite דרך הספרייה הסטנדרטית.

הסכימה קטנה ויציבה, ולכן SQL ישיר קריא יותר מ-ORM כאן. כל התאריכים נשמרים
כמחרוזות ISO-8601 ב-UTC, וההמרה לאזור הזמן המקומי נעשית רק בתצוגה.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    sku           TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    standard_qty  INTEGER NOT NULL CHECK (standard_qty >= 0),
    active        INTEGER NOT NULL DEFAULT 1,
    notes         TEXT,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS issuances (
    id          INTEGER PRIMARY KEY,
    -- מפתח הדדופליקציה: מייל שנקלט לא ייספר פעמיים לעולם.
    message_id  TEXT    NOT NULL UNIQUE,
    email_date  TEXT    NOT NULL,
    recipient   TEXT,
    issuer      TEXT,
    center      TEXT,
    raw_text    TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('applied', 'needs_review', 'ignored')),
    source      TEXT    NOT NULL CHECK (source IN ('email', 'paste')),
    review_note TEXT,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS issuance_lines (
    id           INTEGER PRIMARY KEY,
    issuance_id  INTEGER NOT NULL REFERENCES issuances(id) ON DELETE CASCADE,
    raw_sku      TEXT    NOT NULL,
    raw_name     TEXT    NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    item_id      INTEGER REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS adjustments (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    -- delta חיובי = מלאי שנוסף (אספקה/החזרה), ולכן מקטין את החוסר.
    delta      INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('edit', 'reset')),
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS import_runs (
    id             INTEGER PRIMARY KEY,
    filename       TEXT    NOT NULL,
    created_count  INTEGER NOT NULL DEFAULT 0,
    updated_count  INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    report         TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_lines_issuance ON issuance_lines(issuance_id);
CREATE INDEX IF NOT EXISTS ix_lines_item     ON issuance_lines(item_id);
CREATE INDEX IF NOT EXISTS ix_adj_item       ON adjustments(item_id);
CREATE INDEX IF NOT EXISTS ix_iss_status     ON issuances(status);
"""

_local = threading.local()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def connect() -> sqlite3.Connection:
    """
    חיבור לכל thread בנפרד (השרת מרובה-threads ויש גם thread של התזמון).
    WAL מאפשר קריאות במקביל לכתיבה.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    path = config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """טרנזקציה אחת. כישלון באמצע מגלגל הכל אחורה."""
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)


def reset_for_tests() -> None:
    """סוגר את החיבור של ה-thread הנוכחי — לשימוש הטסטים בלבד."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
