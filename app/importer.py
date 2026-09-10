"""
Import of the standard file — CSV or Excel.

The import runs in three separate steps, and the split is the whole point:

    scan()     reads the file and validates it. Touches nothing in the database.
    compare()  puts what the file says next to what the system currently holds.
    apply()    writes — and only after the user has approved the comparison.

That is what lets the interface show what is about to change before anything
changes, instead of reporting it afterwards when it is too late to object.

Four columns are read: SKU, item name, the approved standard, and the stock in
the cupboard. Everything else in the report — shortage, quantity on order,
status text — is the output of a calculation that this system performs itself,
and is ignored on purpose.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from app import inventory, repo, spreadsheet
from app.db import transaction, utcnow
from app.parsing.normalize import clean_text, normalize_sku

COL_SKU = 'מק"ט'
COL_NAME = "שם פריט"

# The standard column and the stock column have each carried more than one name
# across versions of the report, and both versions are still in circulation. The
# first name found in the file wins.
COLS_STANDARD = ("תקן מאושר", "תקן")
#: 'מלאי לאחר הגעת משלוח' comes first deliberately: where a file holds both, it
#: is the later of the two — the count once the delivery has been put away.
COLS_STOCK = ("מלאי לאחר הגעת משלוח", "מלאי עדכני")

# Read and discarded on purpose — documented here so they are not picked up by
# mistake later on. 'מלאי נוכחי' is the stock *before* the delivery arrived,
# which is exactly the number that must not be imported.
IGNORED_COLUMNS = ("מלאי נוכחי", "חוסר נטו", "כמות חוסר", "כמות להזמנה", "כמות בהזמנה נוכחית", "הגיע בפועל", "סטטוס והסבר", "הסבר")

#: The movement kind recorded for a stock update from a file — the same kind a
#: manual stock count produces, because that is exactly what it is.
KIND_EDIT = "edit"


# ------------------------------------------------------------------ the scan


@dataclass(frozen=True)
class ScannedRow:
    """One valid row of the file. Nothing here has been written anywhere yet."""

    sku: str
    name: str
    standard: int
    #: The stock the file reports for the cupboard. None = the file says nothing
    #: about this item's stock, and the stock in the system stays as it is.
    stock: int | None = None


@dataclass
class Scan:
    filename: str = "import.csv"
    rows: list[ScannedRow] = field(default_factory=list)
    rejected: int = 0
    problems: list[str] = field(default_factory=list)
    #: Whether the file carried a stock column at all.
    has_stock: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.rows)


# --------------------------------------------------------- the comparison


@dataclass(frozen=True)
class ComparedRow:
    """One row of the file, against what the system currently holds for it."""

    sku: str
    name: str
    is_new: bool
    standard: int
    standard_before: int | None
    stock: int | None
    stock_before: int | None
    name_before: str | None = None

    @property
    def effective_before(self) -> int:
        """
        The stock to compare against. A new item has no stock yet, and once
        created it starts full at its standard — so that is what it starts from.
        """
        return self.standard if self.is_new else (self.stock_before or 0)

    @property
    def stock_delta(self) -> int:
        return 0 if self.stock is None else self.stock - self.effective_before

    @property
    def stock_changed(self) -> bool:
        return self.stock is not None and self.stock_delta != 0

    @property
    def standard_changed(self) -> bool:
        return self.standard_before is not None and self.standard != self.standard_before

    @property
    def renamed(self) -> bool:
        return self.name_before is not None and self.name != self.name_before

    @property
    def changed(self) -> bool:
        return self.is_new or self.stock_changed or self.standard_changed or self.renamed


@dataclass
class Comparison:
    """What the file would do to the system, before a single row is written."""

    filename: str
    rows: list[ComparedRow] = field(default_factory=list)
    #: Active items the system holds and the file never mentions. They are left
    #: untouched — they are listed so that nobody has to discover that later.
    missing: list[repo.Item] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    rejected: int = 0
    has_stock: bool = False

    @property
    def changed_rows(self) -> list[ComparedRow]:
        return [r for r in self.rows if r.changed]

    @property
    def unchanged_count(self) -> int:
        return len(self.rows) - len(self.changed_rows)

    @property
    def new_count(self) -> int:
        return sum(1 for r in self.rows if r.is_new)

    @property
    def stock_change_count(self) -> int:
        return sum(1 for r in self.rows if r.stock_changed)

    @property
    def standard_change_count(self) -> int:
        return sum(1 for r in self.rows if r.standard_changed)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_rows)


# ------------------------------------------------------------- the result


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    rejected: int = 0
    #: Items whose cupboard stock was actually moved by the import.
    stock_updated: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def total_ok(self) -> int:
        return self.created + self.updated

    def summary(self) -> str:
        parts = [f"נוספו {self.created}", f"עודכנו {self.updated}"]
        if self.stock_updated:
            parts.append(f"מלאי עודכן ל-{self.stock_updated}")
        if self.rejected:
            parts.append(f"נדחו {self.rejected}")
        return " · ".join(parts)


# ------------------------------------------------------------ reading rows


def _decode(content: bytes | str) -> str:
    # utf-8-sig strips the BOM present in the file exported from the report.
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Hebrew Excel sometimes exports as cp1255.
            return content.decode("cp1255", errors="replace")
    return content.lstrip("﻿")


def _raw_rows(content: bytes | str, filename: str) -> list[dict[str, str]]:
    """
    The rows of the file, whichever of the two formats it is in.

    The choice is made by the content and not by the file name: a file renamed
    to .csv is still a zip, and reading it as text would produce nonsense.
    """
    if isinstance(content, bytes) and spreadsheet.looks_like_xlsx(content):
        return spreadsheet.read_rows(content)
    rows = list(csv.DictReader(io.StringIO(_decode(content))))
    # csv.DictReader keeps the header exactly as written; the sheet reader
    # strips it, and the two have to agree.
    return [{(k or "").strip(): (v or "") for k, v in row.items()} for row in rows]


def _pick_column(header: list[str], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in header), None)


# ------------------------------------------------------------------- scan


def scan(content: bytes | str, filename: str = "import.csv") -> Scan:
    """
    Reads and validates the file. Writes nothing, anywhere — every problem comes
    back in `problems`, in Hebrew, ready to be shown as it is.
    """
    result = Scan(filename=filename)

    try:
        rows = _raw_rows(content, filename)
    except spreadsheet.SpreadsheetError:
        result.problems.append("לא ניתן לקרוא את קובץ ה-Excel. יש לשמור אותו מחדש כ-xlsx או כ-CSV.")
        return result

    if not rows:
        result.problems.append("הקובץ ריק או שאין בו שורות נתונים.")
        return result

    header = list(rows[0])
    standard_column = _pick_column(header, COLS_STANDARD)
    stock_column = _pick_column(header, COLS_STOCK)
    result.has_stock = stock_column is not None

    missing = [COL_SKU] if COL_SKU not in header else []
    if COL_NAME not in header:
        missing.append(COL_NAME)
    if standard_column is None:
        missing.append(" או ".join(COLS_STANDARD))
    if missing:
        result.problems.append("חסרות עמודות חובה בקובץ: " + ", ".join(missing))
        result.rejected = len(rows)
        return result

    seen: dict[str, int] = {}
    for line_no, row in enumerate(rows, start=2):  # line 1 is the header row
        sku = normalize_sku(row.get(COL_SKU))
        name = clean_text(row.get(COL_NAME))
        raw_standard = clean_text(row.get(standard_column))

        if not sku:
            result.rejected += 1
            result.problems.append(f'שורה {line_no}: מק"ט ריק — נדחתה.')
            continue
        if not name:
            result.rejected += 1
            result.problems.append(f'שורה {line_no}: שם פריט ריק (מק"ט {sku}) — נדחתה.')
            continue
        try:
            standard = int(raw_standard)
        except (TypeError, ValueError):
            result.rejected += 1
            result.problems.append(f'שורה {line_no}: תקן לא מספרי ("{raw_standard}", מק"ט {sku}) — נדחתה.')
            continue
        if standard < 0:
            result.rejected += 1
            result.problems.append(f"שורה {line_no}: תקן שלילי ({standard}, מק\"ט {sku}) — נדחתה.")
            continue
        if sku in seen:
            # Nothing is overwritten silently: a duplicate SKU in the file is a
            # mistake that has to be seen.
            result.rejected += 1
            result.problems.append(f'שורה {line_no}: מק"ט {sku} מופיע כבר בשורה {seen[sku]} — נדחתה.')
            continue

        seen[sku] = line_no
        stock = _scan_stock(row, stock_column, line_no, sku, result)
        result.rows.append(ScannedRow(sku=sku, name=name, standard=standard, stock=stock))

    return result


def _scan_stock(row: dict[str, str], column: str | None, line_no: int, sku: str, result: Scan) -> int | None:
    """
    The stock reported for the row, or None when the file says nothing about it.

    A stock that cannot be read does not throw the row away — the item and its
    standard are still worth importing. Only the stock is left alone, and it is
    said out loud rather than passed over.
    """
    if column is None:
        return None
    raw = clean_text(row.get(column))
    if not raw:
        return None
    try:
        stock = int(raw)
    except (TypeError, ValueError):
        result.problems.append(f'שורה {line_no}: מלאי לא מספרי ("{raw}", מק"ט {sku}) — הפריט יובא בלי עדכון מלאי.')
        return None
    if stock < 0:
        result.problems.append(f'שורה {line_no}: מלאי שלילי ({stock}, מק"ט {sku}) — הפריט יובא בלי עדכון מלאי.')
        return None
    return stock


# ------------------------------------------------- storing a pending scan


def to_payload(scanned: Scan) -> dict:
    """The scan as plain data, for the wait between the upload and the approval."""
    return {
        "rows": [{"sku": r.sku, "name": r.name, "standard": r.standard, "stock": r.stock} for r in scanned.rows],
        "rejected": scanned.rejected,
        "problems": scanned.problems,
        "has_stock": scanned.has_stock,
    }


def from_payload(filename: str, payload: dict) -> Scan:
    """
    The other direction. Deliberately forgiving: what was stored may have been
    written by an earlier version of the code, and a waiting import is not
    worth an error screen — a field that cannot be read is simply dropped.
    """
    rows = []
    for raw in payload.get("rows", []):
        try:
            stock = raw.get("stock")
            rows.append(
                ScannedRow(
                    sku=str(raw["sku"]),
                    name=str(raw["name"]),
                    standard=int(raw["standard"]),
                    stock=None if stock is None else int(stock),
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return Scan(
        filename=filename,
        rows=rows,
        rejected=int(payload.get("rejected") or 0),
        problems=[str(p) for p in payload.get("problems", [])],
        has_stock=bool(payload.get("has_stock")),
    )


# ---------------------------------------------------------------- compare


def compare(scanned: Scan) -> Comparison:
    """The file against the current state of the system. Reads only."""
    existing = {item.sku: item for item in repo.list_items()}
    remaining = {s.item.id: s.remaining for s in inventory.status_for_all(list(existing.values()))}

    rows = []
    for row in scanned.rows:
        item = existing.get(row.sku)
        rows.append(
            ComparedRow(
                sku=row.sku,
                name=row.name,
                is_new=item is None,
                standard=row.standard,
                standard_before=item.standard_qty if item else None,
                stock=row.stock,
                stock_before=remaining.get(item.id) if item else None,
                name_before=item.name if item else None,
            )
        )

    in_file = {row.sku for row in scanned.rows}
    return Comparison(
        filename=scanned.filename,
        rows=rows,
        # Only active items: one that was switched off on purpose is not news.
        missing=[i for i in existing.values() if i.active and i.sku not in in_file],
        problems=list(scanned.problems),
        rejected=scanned.rejected,
        has_stock=scanned.has_stock,
    )


# ------------------------------------------------------------------ apply


def apply(scanned: Scan, with_stock: bool = True) -> ImportResult:
    """
    Writes the file into the system. Upsert by SKU, so a repeat import updates
    the standard quantities and the names without losing the issuance history.

    With `with_stock`, the cupboard stock is set to what the file reports, and
    the difference is recorded as an ordinary movement — the same record a
    manual stock count leaves behind, visible in the movements log.
    """
    result = ImportResult(rejected=scanned.rejected, problems=list(scanned.problems))
    if not scanned.rows:
        repo.add_import_run(scanned.filename, 0, 0, result.rejected, "\n".join(result.problems))
        return result

    existing = {item.sku: item for item in repo.list_items()}
    to_create = [(r.sku, r.name, r.standard) for r in scanned.rows if r.sku not in existing]
    to_update = [(r.name, r.standard, existing[r.sku].id) for r in scanned.rows if r.sku in existing]
    result.created = len(to_create)
    result.updated = len(to_update)

    stamp = utcnow()
    with transaction() as conn:
        if to_create:
            conn.executemany(
                "INSERT INTO items (sku, name, standard_qty, active, created_at) VALUES (?, ?, ?, 1, ?)",
                [(sku, name, std, stamp) for sku, name, std in to_create],
            )
        if to_update:
            conn.executemany("UPDATE items SET name = ?, standard_qty = ?, active = 1 WHERE id = ?", to_update)

    if with_stock:
        result.stock_updated = _apply_stock(scanned)

    repo.add_import_run(scanned.filename, result.created, result.updated, result.rejected, "\n".join(result.problems))
    return result


def _apply_stock(scanned: Scan) -> int:
    """
    Brings each item's stock to what the file reports, as a movement per item.

    Deliberately computed *after* the items have been written, and from the
    stock as it stands at this moment rather than from what the comparison
    showed: an issuance email may well have arrived while the comparison was on
    screen. The file states how much is in the cupboard, and that is the figure
    the system must end up at.
    """
    targets = {row.sku: row.stock for row in scanned.rows if row.stock is not None}
    if not targets:
        return 0

    items = [item for item in repo.list_items() if item.sku in targets]
    reason = f"ייבוא מקובץ {scanned.filename}"[:200]
    movements = []
    for status in inventory.status_for_all(items):
        delta = targets[status.item.sku] - status.remaining
        if delta:  # an item already at the right quantity leaves no record
            movements.append((status.item.id, delta, reason, KIND_EDIT))

    if movements:
        repo.add_adjustments(movements)
    return len(movements)


# ----------------------------------------------------------- the old path


def import_items(content: bytes | str, filename: str = "import.csv", with_stock: bool = False) -> ImportResult:
    """
    Scan and apply in one go, with no confirmation in between.

    Used by the initial import at startup, which has nobody to ask. It leaves
    the stock alone by default, so a fresh installation still starts full at the
    standard quantity — the stock only moves when a person uploads a file and
    approves the comparison.
    """
    return apply(scan(content, filename), with_stock=with_stock)
