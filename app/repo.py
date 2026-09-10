"""Data access. All of the system SQL lives here rather than scattered across the screens."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.db import Row, connect, insert_returning_id, parse_dt, utcnow
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
    def from_row(row: Row) -> "Item":
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
    Matching by SKU only — an exact match after normalization, never a guess.

    Normalization is applied on both sides (on import and on lookup), so ' 11-11 '
    in an email finds the item stored as '1111'.
    """
    key = normalize_sku(raw_sku)
    if not key:
        return None
    row = connect().execute("SELECT * FROM items WHERE sku = ?", (key,)).fetchone()
    return Item.from_row(row) if row else None


def create_item(sku: str, name: str, standard_qty: int, conn: Any | None = None) -> int:
    conn = conn or connect()
    return insert_returning_id(
        conn,
        "INSERT INTO items (sku, name, standard_qty, active, created_at) VALUES (?, ?, ?, 1, ?)",
        (normalize_sku(sku), name, max(0, standard_qty), utcnow()),
    )


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
        """The name in the email differs from the one in the file — shown for the record, not as a warning."""
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
    #: When the issuance entered the database — as opposed to `email_date`,
    #: which is when the email was sent. Needed to tell whether a stock movement
    #: was recorded while this issuance was already in the count.
    created_at: datetime | None = None


def _issuance_from_row(row: Row) -> Issuance:
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
        created_at=parse_dt(row["created_at"]),
    )


def _attach_lines(issuances: list[Issuance]) -> list[Issuance]:
    """Loads all the lines in a single query, not one per issuance."""
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
    content_key: str | None = None,
) -> int:
    """
    Writes an issuance and its lines in a single transaction — either all of it
    lands, or none of it does.
    """
    from app.db import transaction

    with transaction() as conn:
        issuance_id = insert_returning_id(
            conn,
            """
            INSERT INTO issuances
                (message_id, email_date, recipient, issuer, center, raw_text,
                 status, source, review_note, content_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, email_date, recipient, issuer, center, raw_text,
                status, source, review_note, content_key, utcnow(),
            ),
        )
        conn.executemany(
            "INSERT INTO issuance_lines (issuance_id, raw_sku, raw_name, qty, item_id) VALUES (?, ?, ?, ?, ?)",
            [(issuance_id, sku, name, qty, item_id) for sku, name, qty, item_id in lines],
        )
    return issuance_id


def find_applied_with_content(content_key: str, exclude_id: int | None = None) -> Issuance | None:
    """Looks for an already-applied issuance with identical content — to detect a re-forward."""
    if not content_key:
        return None
    sql = "SELECT * FROM issuances WHERE content_key = ? AND status = 'applied'"
    params: list = [content_key]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    row = connect().execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
    return _issuance_from_row(row) if row else None


def set_issuance_content_key(issuance_id: int, content_key: str | None) -> None:
    connect().execute("UPDATE issuances SET content_key = ? WHERE id = ?", (content_key, issuance_id))


def replace_issuance_lines(issuance_id: int, lines: list[tuple[str, str, int, int | None]]) -> None:
    """Replaces the issuance lines — used when re-analysing the stored email body."""
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


def set_issuances_status(issuance_ids: list[int], status: str, review_note: str | None) -> int:
    """
    Changes a batch of issuances in one transaction, so a half-finished run
    cannot leave the stock count in a state nobody asked for.
    """
    if not issuance_ids:
        return 0
    from app.db import transaction

    with transaction() as conn:
        conn.executemany(
            "UPDATE issuances SET status = ?, review_note = ? WHERE id = ?",
            [(status, review_note, issuance_id) for issuance_id in issuance_ids],
        )
    return len(issuance_ids)


def assign_line_item(line_id: int, item_id: int) -> None:
    connect().execute("UPDATE issuance_lines SET item_id = ? WHERE id = ?", (item_id, line_id))


def get_line(line_id: int) -> Row | None:
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
    return insert_returning_id(
        connect(),
        "INSERT INTO adjustments (item_id, delta, reason, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        (item_id, delta, reason, kind, utcnow()),
    )


def add_adjustments(rows: list[tuple[int, int, str, str]]) -> int:
    from app.db import transaction

    stamp = utcnow()
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO adjustments (item_id, delta, reason, kind, created_at) VALUES (?, ?, ?, ?, ?)",
            [(item_id, delta, reason, kind, stamp) for item_id, delta, reason, kind in rows],
        )
    return len(rows)


def earliest_movement_at() -> datetime | None:
    """
    When the count was first settled by hand — the earliest stock movement.

    An issuance that entered the database after this point was subtracted from a
    quantity somebody had already asserted, which is what makes it a double
    count. See `ingest.cancel_double_counted`.
    """
    row = connect().execute("SELECT MIN(created_at) AS first_movement FROM adjustments").fetchone()
    return parse_dt(row["first_movement"] if row else None)


def items_with_movements_since(item_ids: list[int], moment: datetime) -> set[int]:
    """
    Which of these items had a stock movement recorded from this moment on.

    Those are the items whose asserted quantity already absorbed the issuance
    about to be cancelled, so cancelling it leaves them too high by its
    quantity — they need a fresh stock count.
    """
    if not item_ids:
        return set()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    placeholders = ",".join("?" * len(item_ids))
    rows = connect().execute(
        f"SELECT DISTINCT item_id FROM adjustments "
        f"WHERE item_id IN ({placeholders}) AND created_at > ?",
        (*item_ids, moment.astimezone(timezone.utc).isoformat(timespec="seconds")),
    )
    return {row["item_id"] for row in rows}


def count_movements_since(moment: datetime) -> int:
    """
    How many manual stock movements were recorded from this moment on.

    Every `created_at` in this table is written by `utcnow()`, so it is always
    UTC in the same shape and comparing the strings is sound.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    row = connect().execute(
        "SELECT COUNT(*) AS n FROM adjustments WHERE created_at >= ?",
        (moment.astimezone(timezone.utc).isoformat(timespec="seconds"),),
    ).fetchone()
    return int(row["n"])


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


# ----------------------------------------------------------------- settings

#: Emails dated before this moment are recorded but kept out of the stock
#: count — see `ingest.before_intake_cutoff`.
INTAKE_CUTOFF = "intake_cutoff"


def get_setting(key: str) -> str | None:
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    """
    An empty value deletes the row, so "not set" has exactly one representation
    and no caller has to distinguish a missing row from an empty string.

    Delete-then-insert rather than an upsert, because it reads the same in both
    engines — see the note at the top of app/db.py.
    """
    from app.db import transaction

    with transaction() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        if value:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, utcnow()),
            )


def get_intake_cutoff() -> datetime | None:
    return parse_dt(get_setting(INTAKE_CUTOFF))


def set_intake_cutoff(moment: datetime | None) -> None:
    """None removes the cutoff, and intake goes back to accepting any date."""
    if moment is None:
        set_setting(INTAKE_CUTOFF, None)
        return
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    set_setting(INTAKE_CUTOFF, moment.astimezone(timezone.utc).isoformat(timespec="seconds"))


def last_reset_at() -> datetime | None:
    """
    When the most recent reset to standard was recorded.

    Offered on the screen as the cutoff date, because that reset is exactly the
    moment from which the past stopped being relevant to the count.
    """
    row = connect().execute(
        "SELECT MAX(created_at) AS last_reset FROM adjustments WHERE kind = ?", ("reset",)
    ).fetchone()
    return parse_dt(row["last_reset"] if row else None)


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


# ---------------------------------------------------------- pending import


@dataclass(frozen=True)
class PendingImport:
    """A scanned file waiting for the user to approve the comparison."""

    filename: str
    payload: dict
    created_at: datetime | None


def save_pending_import(filename: str, payload: dict) -> None:
    """
    Stores the scan, replacing any earlier one. At most one file waits at a
    time — a second upload is a change of mind about the first, not a queue.
    """
    from app.db import transaction

    stamp = utcnow()
    with transaction() as conn:
        conn.execute("DELETE FROM pending_imports")
        conn.execute(
            "INSERT INTO pending_imports (filename, payload, created_at) VALUES (?, ?, ?)",
            (filename, json.dumps(payload, ensure_ascii=False), stamp),
        )


def get_pending_import() -> PendingImport | None:
    row = connect().execute("SELECT * FROM pending_imports ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        # Stored content that cannot be read is not worth bringing a screen down
        # over — the user simply uploads the file again.
        return None
    return PendingImport(filename=row["filename"], payload=payload, created_at=parse_dt(row["created_at"]))


def clear_pending_import() -> None:
    connect().execute("DELETE FROM pending_imports")
