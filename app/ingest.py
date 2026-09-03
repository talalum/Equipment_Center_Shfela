"""
Taking an issuance into the database, and recording manual stock movements.

The two golden rules:
  1. Deduplication by Message-ID — an email is never counted twice.
  2. Intake is all-or-nothing — an issuance with a single problematic line waits
     for review as a whole, because a partial intake silently produces wrong stock.
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
    A fingerprint of the issuance *content*: recipient, issuer, center, and the
    list of SKUs with their quantities.

    Needed because a Message-ID identifies the email, not the issuance: every
    re-forward of the same issuance gets a new id, and so used to be counted
    again. The date is deliberately *not* included — a forward arrives with a
    date different from the original, and including it would miss exactly the
    case we are trying to catch.
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
    An id for an email pasted by hand, which has no Message-ID.

    Derived from the email content, so pasting the same text twice is not
    counted twice.
    """
    digest = hashlib.sha256(raw_text.strip().encode("utf-8")).hexdigest()[:32]
    return f"paste-{digest}"


def _classify(
    parsed: issuance_parser.ParsedIssuance, fmt: issuance_parser.EmailFormat
) -> tuple[str, str | None, str, list[tuple[str, str, int, int | None]]]:
    """
    Decides what to do with a parsed issuance, and matches each line to an item
    by SKU.

    Shared by the first intake and by re-analysis, so that the two paths never
    diverge.

    The order of the decisions matters: the center check used to run first, so
    an email that had not parsed at all was labelled "unknown center" and marked
    ignored — that is, it vanished silently behind a misleading message.
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
    Re-analyses an issuance already in the database, from the original email
    body that was stored with it.

    Needed after an improvement to the parser: deduplication by Message-ID
    prevents the same email from being fetched again, so without this action a
    fix in the code would never reach emails already taken in — they would stay
    stuck with the old error.
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
    Re-analyses every issuance that has not been applied to stock.

    Issuances already applied are left alone — they are fine, and re-analysing
    them could change existing stock without anyone asking for it.
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
    """Takes in a single email. Returns a result even when the issuance was not
    applied — the status explains why."""
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

    # An issuance that looks identical to one already applied is neither applied
    # nor discarded on its own — it goes to a manual decision, because the email
    # cannot tell us whether this is a re-forward or genuinely a second issuance
    # of the same equipment to the same person.
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
    """Approves an issuance that was waiting for review. Fails if a line is still unmatched."""
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
    An edit: the user types the quantity actually counted, not a difference.
    The difference is computed here and stored as a movement. A zero delta
    creates no needless record.
    """
    current = inventory.status_for_item(item).remaining
    delta = actual_qty - current
    if delta == 0:
        return None
    repo.add_adjustment(item.id, delta, reason.strip() or "עדכון ידני", KIND_EDIT)
    return delta


def record_reset(item: repo.Item, reason: str = "איפוס לתקן") -> int | None:
    """Reset to standard: brings the item back to exactly its standard quantity. Idempotent."""
    current = inventory.status_for_item(item).remaining
    delta = item.standard_qty - current
    if delta == 0:
        return None
    repo.add_adjustment(item.id, delta, reason, KIND_RESET)
    return delta


def reset_all_shortages() -> int:
    """Reset to standard for every item in shortage. Returns how many items changed."""
    pending = [
        (s.item.id, s.item.standard_qty - s.remaining, "איפוס לתקן (גורף)", KIND_RESET)
        for s in inventory.status_for_all(repo.list_items(include_inactive=False))
        if s.in_shortage
    ]
    if not pending:
        return 0
    return repo.add_adjustments(pending)
