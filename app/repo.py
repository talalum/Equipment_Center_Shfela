"""גישה לנתונים. כל ה-SQL של המערכת יושב כאן ולא מפוזר במסכים."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.db import connect, parse_dt, utcnow
from app.parsing.normalize import normalize_sku


# --------------------------------------------------------------------- items


@dataclass(frozen=True)
class Item:
    id: int
    sku: str
    name: str
    standard_qty: int
    active: bool

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Item":
        return Item(
            id=row["id"],
            sku=row["sku"],
            name=row["name"],
            standard_qty=row["standard_qty"],
            active=bool(row["active"]),
        )


def list_items(include_inactive: bool = True) -> list[Item]:
    sql = "SELECT * FROM items"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY sku"
    return [Item.from_row(r) for r in connect().execute(sql)]


def get_item(item_id: int) -> Item | None:
    row = connect().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return Item.from_row(row) if row else None


def find_item_by_sku(raw_sku: str) -> Item | None:
    """
    שיוך לפי מק"ט בלבד — התאמה מדויקת אחרי נרמול, לא ניחוש.

    הנרמול מופעל על שני הצדדים (בייבוא ובחיפוש), ולכן ' 11-11 ' במייל
    ימצא את הפריט שנשמר כ-'1111'.
    """
    key = normalize_sku(raw_sku)
    if not key:
        return None
    row = connect().execute("SELECT * FROM items WHERE sku = ?", (key,)).fetchone()
    return Item.from_row(row) if row else None


def create_item(sku: str, name: str, standard_qty: int, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    cur = conn.execute(
        "INSERT INTO items (sku, name, standard_qty, active, created_at) VALUES (?, ?, ?, 1, ?)",
        (normalize_sku(sku), name, max(0, standard_qty), utcnow()),
    )
    return int(cur.lastrowid)


def update_item(item_id: int, name: str, standard_qty: int, active: bool) -> None:
    connect().execute(
        "UPDATE items SET name = ?, standard_qty = ?, active = ? WHERE id = ?",
        (name, max(0, standard_qty), 1 if active else 0, item_id),
    )


# ----------------------------------------------------------------- issuances


@dataclass
class IssuanceLine:
    id: int
    raw_sku: str
    raw_name: str
    qty: int
    item_id: int | None
    item_name: str | None
    item_sku: str | None

    @property
    def matched(self) -> bool:
        return self.item_id is not None

    @property
    def name_differs(self) -> bool:
        """השם במייל שונה מהשם בקובץ — מוצג לתיעוד, לא כאזהרה."""
        return bool(self.item_name) and self.item_name != self.raw_name


@dataclass
class Issuance:
    id: int
    message_id: str
    email_date: datetime | None
    recipient: str | None
    issuer: str | None
    center: str | None
    raw_text: str
    status: str
    source: str
    review_note: str | None
    lines: list[IssuanceLine]


def _issuance_from_row(row: sqlite3.Row) -> Issuance:
    return Issuance(
        id=row["id"],
        message_id=row["message_id"],
        email_date=parse_dt(row["email_date"]),
        recipient=row["recipient"],
        issuer=row["issuer"],
        center=row["center"],
        raw_text=row["raw_text"],
        status=row["status"],
        source=row["source"],
        review_note=row["review_note"],
        lines=[],
    )


def _attach_lines(issuances: list[Issuance]) -> list[Issuance]:
    """טוען את כל השורות בשאילתה אחת, לא אחת לכל הנפקה."""
    if not issuances:
        return issuances
    by_id = {i.id: i for i in issuances}
    placeholders = ",".join("?" * len(by_id))
    rows = connect().execute(
        f"""
        SELECT l.*, i.name AS item_name, i.sku AS item_sku
        FROM issuance_lines l
        LEFT JOIN items i ON i.id = l.item_id
        WHERE l.issuance_id IN ({placeholders})
        ORDER BY l.id
        """,
        tuple(by_id),
    )
    for row in rows:
        by_id[row["issuance_id"]].lines.append(
            IssuanceLine(
                id=row["id"],
                raw_sku=row["raw_sku"],
                raw_name=row["raw_name"],
                qty=row["qty"],
                item_id=row["item_id"],
                item_name=row["item_name"],
                item_sku=row["item_sku"],
            )
        )
    return issuances


def get_issuance(issuance_id: int) -> Issuance | None:
    row = connect().execute("SELECT * FROM issuances WHERE id = ?", (issuance_id,)).fetchone()
    if not row:
        return None
    return _attach_lines([_issuance_from_row(row)])[0]


def find_issuance_by_message_id(message_id: str) -> Issuance | None:
    row = connect().execute("SELECT * FROM issuances WHERE message_id = ?", (message_id,)).fetchone()
    return _issuance_from_row(row) if row else None


def list_issuances(statuses: tuple[str, ...], limit: int = 200, newest_first: bool = True) -> list[Issuance]:
    placeholders = ",".join("?" * len(statuses))
    order = "DESC" if newest_first else "ASC"
    rows = connect().execute(
        f"SELECT * FROM issuances WHERE status IN ({placeholders}) "
        f"ORDER BY email_date {order}, id {order} LIMIT ?",
        (*statuses, limit),
    )
    return _attach_lines([_issuance_from_row(r) for r in rows])


def count_issuances(status: str) -> int:
    row = connect().execute("SELECT COUNT(*) AS n FROM issuances WHERE status = ?", (status,)).fetchone()
    return int(row["n"])


def insert_issuance(
    message_id: str,
    email_date: str,
    recipient: str | None,
    issuer: str | None,
    center: str | None,
    raw_text: str,
    status: str,
    source: str,
    review_note: str | None,
    lines: list[tuple[str, str, int, int | None]],
) -> int:
    """
    כותב הנפקה ואת שורותיה בטרנזקציה אחת — או שהכול נכנס, או שכלום לא.
    """
    from app.db import transaction

    with transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO issuances
                (message_id, email_date, recipient, issuer, center, raw_text,
                 status, source, review_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, email_date, recipient, issuer, center, raw_text,
                status, source, review_note, utcnow(),
            ),
        )
        issuance_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO issuance_lines (issuance_id, raw_sku, raw_name, qty, item_id) VALUES (?, ?, ?, ?, ?)",
            [(issuance_id, sku, name, qty, item_id) for sku, name, qty, item_id in lines],
        )
    return issuance_id


def replace_issuance_lines(issuance_id: int, lines: list[tuple[str, str, int, int | None]]) -> None:
    """מחליף את שורות ההנפקה — לשימוש בניתוח מחדש של גוף המייל השמור."""
    from app.db import transaction

    with transaction() as conn:
        conn.execute("DELETE FROM issuance_lines WHERE issuance_id = ?", (issuance_id,))
        conn.executemany(
            "INSERT INTO issuance_lines (issuance_id, raw_sku, raw_name, qty, item_id) VALUES (?, ?, ?, ?, ?)",
            [(issuance_id, sku, name, qty, item_id) for sku, name, qty, item_id in lines],
        )


def update_issuance_details(
    issuance_id: int,
    recipient: str | None,
    issuer: str | None,
    center: str | None,
    status: str,
    review_note: str | None,
) -> None:
    connect().execute(
        """
        UPDATE issuances
        SET recipient = ?, issuer = ?, center = ?, status = ?, review_note = ?
        WHERE id = ?
        """,
        (recipient, issuer, center, status, review_note, issuance_id),
    )


def set_issuance_status(issuance_id: int, status: str, review_note: str | None) -> None:
    connect().execute(
        "UPDATE issuances SET status = ?, review_note = ? WHERE id = ?",
        (status, review_note, issuance_id),
    )


def assign_line_item(line_id: int, item_id: int) -> None:
    connect().execute("UPDATE issuance_lines SET item_id = ? WHERE id = ?", (item_id, line_id))


def get_line(line_id: int) -> sqlite3.Row | None:
    return connect().execute("SELECT * FROM issuance_lines WHERE id = ?", (line_id,)).fetchone()


# --------------------------------------------------------------- adjustments


@dataclass
class Adjustment:
    id: int
    item_id: int
    item_sku: str
    item_name: str
    delta: int
    reason: str
    kind: str
    created_at: datetime | None


def add_adjustment(item_id: int, delta: int, reason: str, kind: str) -> int:
    cur = connect().execute(
        "INSERT INTO adjustments (item_id, delta, reason, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        (item_id, delta, reason, kind, utcnow()),
    )
    return int(cur.lastrowid)


def add_adjustments(rows: list[tuple[int, int, str, str]]) -> int:
    from app.db import transaction

    stamp = utcnow()
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO adjustments (item_id, delta, reason, kind, created_at) VALUES (?, ?, ?, ?, ?)",
            [(item_id, delta, reason, kind, stamp) for item_id, delta, reason, kind in rows],
        )
    return len(rows)


def list_adjustments(limit: int = 300) -> list[Adjustment]:
    rows = connect().execute(
        """
        SELECT a.*, i.sku AS item_sku, i.name AS item_name
        FROM adjustments a JOIN items i ON i.id = a.item_id
        ORDER BY a.created_at DESC, a.id DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        Adjustment(
            id=r["id"],
            item_id=r["item_id"],
            item_sku=r["item_sku"],
            item_name=r["item_name"],
            delta=r["delta"],
            reason=r["reason"],
            kind=r["kind"],
            created_at=parse_dt(r["created_at"]),
        )
        for r in rows
    ]


# -------------------------------------------------------------- import runs


@dataclass
class ImportRun:
    filename: str
    created_count: int
    updated_count: int
    rejected_count: int
    report: str
    created_at: datetime | None


def add_import_run(filename: str, created: int, updated: int, rejected: int, report: str) -> None:
    connect().execute(
        """
        INSERT INTO import_runs (filename, created_count, updated_count, rejected_count, report, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (filename, created, updated, rejected, report, utcnow()),
    )


def last_import_run() -> ImportRun | None:
    row = connect().execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return ImportRun(
        filename=row["filename"],
        created_count=row["created_count"],
        updated_count=row["updated_count"],
        rejected_count=row["rejected_count"],
        report=row["report"],
        created_at=parse_dt(row["created_at"]),
    )
