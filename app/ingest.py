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
from app.parsing.normalize import clean_text

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


def content_fingerprint(parsed: issuance_parser.ParsedIssuance) -> str:
    """
    טביעת אצבע של *תוכן* ההנפקה: מקבל, מנפיק, מרכז, ורשימת מק"טים וכמויות.

    נחוצה כי Message-ID אינו מזהה את ההנפקה אלא את המייל: כל העברה חוזרת
    של אותה הנפקה מקבלת מזהה חדש, ולכן הייתה נספרת שוב. התאריך *לא* נכלל
    בכוונה — העברה מגיעה בתאריך אחר מהמקור, והכללתו הייתה מחטיאה בדיוק
    את המקרה שאנחנו מנסים לתפוס.
    """
    items = "|".join(sorted(f"{line.normalized_sku}:{line.qty}" for line in parsed.lines))
    parts = [
        clean_text(parsed.recipient or ""),
        clean_text(parsed.issuer or ""),
        clean_text(parsed.center or ""),
        items,
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:32]


def _duplicate_note(existing) -> str:
    when = existing.email_date.strftime("%d/%m/%Y %H:%M") if existing.email_date else "תאריך לא ידוע"
    return (
        f"נראה כמו אותה הנפקה שכבר נקלטה ({when}) — ייתכן שהמייל הועבר שוב.\n"
        "אם זו באמת הנפקה נוספת ונפרדת — לאשר. אם זו העברה חוזרת — להתעלם."
    )


def synthetic_message_id(raw_text: str) -> str:
    """
    מזהה למייל שהודבק ידנית ואין לו Message-ID.

    נגזר מתוכן המייל, ולכן הדבקה כפולה של אותו טקסט לא תיספר פעמיים.
    """
    digest = hashlib.sha256(raw_text.strip().encode("utf-8")).hexdigest()[:32]
    return f"paste-{digest}"


def _classify(
    parsed: issuance_parser.ParsedIssuance, fmt: issuance_parser.EmailFormat
) -> tuple[str, str | None, str, list[tuple[str, str, int, int | None]]]:
    """
    מחליט מה לעשות עם הנפקה שנפרסה, ומשייך כל שורה לפריט לפי מק"ט.

    משותף לקליטה ראשונה ולניתוח מחדש, כדי ששני המסלולים לא יתפצלו לעולם.

    סדר ההחלטה חשוב: בעבר בדיקת המרכז רצה ראשונה, ולכן מייל שלא נפרס כלל
    קיבל "מרכז לא ידוע" וסומן ignored — כלומר נעלם בשקט עם הודעה מטעה.
    """
    notes: list[str] = list(parsed.errors)
    lines: list[tuple[str, str, int, int | None]] = []
    for line in parsed.lines:
        item = repo.find_item_by_sku(line.raw_sku)
        if item is None:
            notes.append(f'מק"ט {line.raw_sku} ("{line.raw_name}") לא קיים במערכת.')
        lines.append((line.raw_sku, line.raw_name, line.qty, item.id if item else None))

    if not parsed.has_items_section:
        return (
            IGNORED,
            "המייל אינו נראה כמו הודעת הנפקה — לא נמצאה בו רשימת מוצרים.",
            "המייל אינו הודעת הנפקה ולכן לא נקלט.",
            lines,
        )
    if parsed.center and not issuance_parser.center_matches(parsed, fmt):
        return (
            IGNORED,
            f'ההנפקה שייכת ל"{parsed.center}" ולא ל"{fmt.expected_center}" — לא נכנסה למלאי.',
            "המייל שייך למרכז ציוד אחר ולכן לא נקלט למלאי.",
            lines,
        )
    if not parsed.center:
        return (
            NEEDS_REVIEW,
            "\n".join([*notes, 'לא זוהתה שורת "מרכז ציוד" במייל — נדרש אישור ידני.']),
            "לא זוהה מרכז הציוד במייל — ממתין לאישור.",
            lines,
        )
    if notes:
        return (
            NEEDS_REVIEW,
            "\n".join(notes),
            "המייל ממתין לאישור — ראי את מסך הביקורת.",
            lines,
        )
    return APPLIED, None, f"נקלטו {len(lines)} פריטים.", lines


def reanalyse_issuance(issuance_id: int) -> tuple[bool, str]:
    """
    מנתח מחדש הנפקה שכבר במסד, לפי גוף המייל המקורי שנשמר.

    נחוץ אחרי שיפור בפרסר: הדדופליקציה לפי Message-ID מונעת משיכה חוזרת
    של אותו מייל, ולכן בלי הפעולה הזו תיקון בקוד לא היה משפיע לעולם על
    מיילים שכבר נקלטו — והם היו נשארים תקועים עם השגיאה הישנה.
    """
    issuance = repo.get_issuance(issuance_id)
    if issuance is None:
        return False, "ההנפקה לא נמצאה."

    fmt = issuance_parser.load_format()
    parsed = issuance_parser.parse(issuance.raw_text, fmt)
    status, note, message, lines = _classify(parsed, fmt)

    content_key = content_fingerprint(parsed) if parsed.lines else None
    if status == APPLIED:
        twin = repo.find_applied_with_content(content_key, exclude_id=issuance_id)
        if twin is not None:
            status = NEEDS_REVIEW
            note = _duplicate_note(twin)
            message = "נראה כהעברה חוזרת של הנפקה שכבר נקלטה — ממתין להכרעה."

    repo.set_issuance_content_key(issuance_id, content_key)
    repo.replace_issuance_lines(issuance_id, lines)
    repo.update_issuance_details(
        issuance_id,
        recipient=parsed.recipient,
        issuer=parsed.issuer,
        center=parsed.center,
        status=status,
        review_note=note,
    )
    changed = status != issuance.status
    prefix = "השתנה: " if changed else "ללא שינוי: "
    return changed, prefix + message


def reanalyse_unapplied() -> dict[str, int]:
    """
    מנתח מחדש את כל ההנפקות שלא נקלטו למלאי.

    לא נוגע בהנפקות שכבר נקלטו — הן תקינות, וניתוח מחדש שלהן היה עלול
    לשנות מלאי קיים בלי שביקשו זאת.
    """
    counts = {"total": 0, "applied": 0, "needs_review": 0, "ignored": 0}
    for issuance in repo.list_issuances((NEEDS_REVIEW, IGNORED), limit=1000):
        reanalyse_issuance(issuance.id)
        refreshed = repo.get_issuance(issuance.id)
        counts["total"] += 1
        counts[refreshed.status] += 1
    return counts


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

    status, note, message, lines = _classify(parsed, fmt)

    # הנפקה שנראית זהה לאחת שכבר נקלטה לא נקלטת לבד ולא נזרקת לבד —
    # היא עוברת להכרעה ידנית, כי אי אפשר לדעת מהמייל אם זו העברה חוזרת
    # או באמת הנפקה שנייה של אותו ציוד לאותו אדם.
    content_key = content_fingerprint(parsed) if parsed.lines else None
    if status == APPLIED:
        twin = repo.find_applied_with_content(content_key)
        if twin is not None:
            status = NEEDS_REVIEW
            note = _duplicate_note(twin)
            message = "המייל נראה כהעברה חוזרת של הנפקה שכבר נקלטה — ממתין להכרעה."

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
        content_key=content_key,
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
