"""
קליטת הנפקה למסד, וביצוע תנועות מלאי ידניות.

שני כללי הזהב:
  1. דדופליקציה לפי Message-ID — מייל לא ייספר פעמיים לעולם.
  2. קליטה היא הכל-או-כלום — הנפקה עם שורה בעייתית אחת ממתינה כולה לביקורת,
     כי קליטה חלקית יוצרת מלאי שגוי בשקט.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from app import inventory, repo
from app.parsing import issuance_parser

APPLIED = "applied"
NEEDS_REVIEW = "needs_review"
IGNORED = "ignored"

KIND_EDIT = "edit"
KIND_RESET = "reset"


@dataclass
class IngestResult:
    issuance_id: int | None
    status: str
    duplicate: bool = False
    message: str = ""


def synthetic_message_id(raw_text: str) -> str:
    """
    מזהה למייל שהודבק ידנית ואין לו Message-ID.

    נגזר מתוכן המייל, ולכן הדבקה כפולה של אותו טקסט לא תיספר פעמיים.
    """
    digest = hashlib.sha256(raw_text.strip().encode("utf-8")).hexdigest()[:32]
    return f"paste-{digest}"


def ingest_issuance(
    raw_text: str,
    message_id: str,
    email_date: datetime | None = None,
    source: str = "email",
) -> IngestResult:
    """קולט מייל אחד. מחזיר תוצאה גם כשההנפקה לא נקלטה — הסטטוס מסביר למה."""
    existing = repo.find_issuance_by_message_id(message_id)
    if existing is not None:
        return IngestResult(
            issuance_id=existing.id,
            status=existing.status,
            duplicate=True,
            message="המייל הזה כבר נקלט במערכת — המלאי לא שונה.",
        )

    fmt = issuance_parser.load_format()
    parsed = issuance_parser.parse(raw_text, fmt)

    notes: list[str] = list(parsed.errors)
    lines: list[tuple[str, str, int, int | None]] = []
    for line in parsed.lines:
        item = repo.find_item_by_sku(line.raw_sku)
        if item is None:
            notes.append(f'מק"ט {line.raw_sku} ("{line.raw_name}") לא קיים במערכת.')
        lines.append((line.raw_sku, line.raw_name, line.qty, item.id if item else None))

    # סדר ההחלטה חשוב. בעבר בדיקת המרכז רצה ראשונה, ולכן מייל שלא נפרס כלל
    # קיבל "מרכז לא ידוע" וסומן ignored — כלומר נעלם בשקט עם הודעה מטעה.
    # עכשיו: רק מייל שברור שאינו הנפקה, או הנפקה של מרכז אחר *שזוהה בוודאות*,
    # מסומן ignored. כל השאר מגיע לביקורת.
    if not parsed.has_items_section:
        status = IGNORED
        note = "המייל אינו נראה כמו הודעת הנפקה — לא נמצאה בו רשימת מוצרים."
        message = "המייל אינו הודעת הנפקה ולכן לא נקלט."
    elif parsed.center and not issuance_parser.center_matches(parsed, fmt):
        status = IGNORED
        note = f'ההנפקה שייכת ל"{parsed.center}" ולא ל"{fmt.expected_center}" — לא נכנסה למלאי.'
        message = "המייל שייך למרכז ציוד אחר ולכן לא נקלט למלאי."
    elif not parsed.center:
        status = NEEDS_REVIEW
        note = "\n".join([*notes, 'לא זוהתה שורת "מרכז ציוד" במייל — נדרש אישור ידני.'])
        message = "לא זוהה מרכז הציוד במייל — ממתין לאישור."
    elif notes:
        status = NEEDS_REVIEW
        note = "\n".join(notes)
        message = "המייל ממתין לאישור — ראי את מסך הביקורת."
    else:
        status = APPLIED
        note = None
        message = f"נקלטו {len(lines)} פריטים."

    stamp = (email_date or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    issuance_id = repo.insert_issuance(
        message_id=message_id,
        email_date=stamp,
        recipient=parsed.recipient,
        issuer=parsed.issuer,
        center=parsed.center,
        raw_text=raw_text,
        status=status,
        source=source,
        review_note=note,
        lines=lines,
    )
    return IngestResult(issuance_id=issuance_id, status=status, message=message)


def approve_issuance(issuance_id: int) -> tuple[bool, str]:
    """מאשר הנפקה שהמתינה לביקורת. נכשל אם נשארה שורה בלי שיוך."""
    issuance = repo.get_issuance(issuance_id)
    if issuance is None:
        return False, "ההנפקה לא נמצאה."
    if issuance.status == APPLIED:
        return False, "ההנפקה כבר נקלטה."
    if not issuance.lines:
        return False, "אין שורות פריטים בהנפקה הזו."
    unmatched = [line.raw_sku for line in issuance.lines if not line.matched]
    if unmatched:
        return False, "עדיין יש שורות בלי שיוך לפריט: " + ", ".join(unmatched)
    repo.set_issuance_status(issuance_id, APPLIED, None)
    return True, f"ההנפקה נקלטה — {len(issuance.lines)} פריטים."


def ignore_issuance(issuance_id: int, note: str = "סומנה ידנית להתעלמות.") -> None:
    repo.set_issuance_status(issuance_id, IGNORED, note)


def record_edit(item: repo.Item, actual_qty: int, reason: str) -> int | None:
    """
    עריכה: המשתמשת מקלידה את הכמות שספרה בפועל, לא הפרש.
    ההפרש מחושב כאן ונשמר כתנועה. delta אפס לא יוצר רשומה מיותרת.
    """
    current = inventory.status_for_item(item).remaining
    delta = actual_qty - current
    if delta == 0:
        return None
    repo.add_adjustment(item.id, delta, reason.strip() or "עדכון ידני", KIND_EDIT)
    return delta


def record_reset(item: repo.Item, reason: str = "איפוס לתקן") -> int | None:
    """איפוס לתקן: מחזיר את הפריט בדיוק לתקן. אידמפוטנטי."""
    current = inventory.status_for_item(item).remaining
    delta = item.standard_qty - current
    if delta == 0:
        return None
    repo.add_adjustment(item.id, delta, reason, KIND_RESET)
    return delta


def reset_all_shortages() -> int:
    """איפוס לתקן לכל הפריטים שבחוסר. מחזיר כמה פריטים שונו."""
    pending = [
        (s.item.id, s.item.standard_qty - s.remaining, "איפוס לתקן (גורף)", KIND_RESET)
        for s in inventory.status_for_all(repo.list_items(include_inactive=False))
        if s.in_shortage
    ]
    if not pending:
        return 0
    return repo.add_adjustments(pending)
