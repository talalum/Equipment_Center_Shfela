"""
ייבוא קובץ התקן.

מהקובץ נלקחות שלוש עמודות בלבד: מק"ט, שם פריט, תקן.
העמודות 'מלאי עדכני', 'כמות חוסר', 'כמות להזמנה', 'הסבר' הן פלט של דוח קיים
ומתעלמות במכוון — הקובץ הוא מקור לרשימת הפריטים ולתקן, לא למצב המלאי.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from app import repo
from app.db import transaction, utcnow
from app.parsing.normalize import clean_text, normalize_sku

COL_SKU = 'מק"ט'
COL_NAME = "שם פריט"
COL_STANDARD = "תקן"
REQUIRED_COLUMNS = (COL_SKU, COL_NAME, COL_STANDARD)

# נקראות ומושלכות במכוון — מתועד כאן כדי שלא ייכנסו בטעות בעתיד.
IGNORED_COLUMNS = ("מלאי עדכני", "כמות חוסר", "כמות להזמנה", "הסבר")


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    rejected: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def total_ok(self) -> int:
        return self.created + self.updated

    def summary(self) -> str:
        parts = [f"נוספו {self.created}", f"עודכנו {self.updated}"]
        if self.rejected:
            parts.append(f"נדחו {self.rejected}")
        return " · ".join(parts)


def _decode(content: bytes | str) -> str:
    # utf-8-sig מסיר את ה-BOM שיש בקובץ שיוצא מהדוח.
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            # אקסל בעברית מייצא לפעמים ב-cp1255.
            return content.decode("cp1255", errors="replace")
    return content.lstrip("﻿")


def import_items(content: bytes | str, filename: str = "import.csv") -> ImportResult:
    """
    upsert לפי מק"ט. ייבוא חוזר מעדכן תקן ושמות בלי לאבד היסטוריית הנפקות,
    כי המק"ט הוא מפתח יציב.
    """
    result = ImportResult()
    rows = list(csv.DictReader(io.StringIO(_decode(content))))

    if not rows:
        result.problems.append("הקובץ ריק או שאין בו שורות נתונים.")
        repo.add_import_run(filename, 0, 0, 0, "\n".join(result.problems))
        return result

    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        result.problems.append("חסרות עמודות חובה בקובץ: " + ", ".join(missing))
        result.rejected = len(rows)
        repo.add_import_run(filename, 0, 0, result.rejected, "\n".join(result.problems))
        return result

    existing = {item.sku: item for item in repo.list_items()}
    seen: dict[str, int] = {}
    to_create: list[tuple[str, str, int]] = []
    to_update: list[tuple[str, int, int]] = []

    for line_no, row in enumerate(rows, start=2):  # שורה 1 היא הכותרות
        sku = normalize_sku(row.get(COL_SKU))
        name = clean_text(row.get(COL_NAME))
        raw_standard = clean_text(row.get(COL_STANDARD))

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
            result.problems.append(f'שורה {line_no}: תקן שלילי ({standard}, מק"ט {sku}) — נדחתה.')
            continue
        if sku in seen:
            # לא דורסים בשקט: מק"ט כפול בקובץ הוא טעות שצריך לראות.
            result.rejected += 1
            result.problems.append(f'שורה {line_no}: מק"ט {sku} מופיע כבר בשורה {seen[sku]} — נדחתה.')
            continue

        seen[sku] = line_no
        item = existing.get(sku)
        if item is None:
            to_create.append((sku, name, standard))
            result.created += 1
        else:
            to_update.append((name, standard, item.id))
            result.updated += 1

    stamp = utcnow()
    with transaction() as conn:
        if to_create:
            conn.executemany(
                "INSERT INTO items (sku, name, standard_qty, active, created_at) VALUES (?, ?, ?, 1, ?)",
                [(sku, name, std, stamp) for sku, name, std in to_create],
            )
        if to_update:
            conn.executemany(
                "UPDATE items SET name = ?, standard_qty = ?, active = 1 WHERE id = ?", to_update
            )

    repo.add_import_run(filename, result.created, result.updated, result.rejected, "\n".join(result.problems))
    return result
