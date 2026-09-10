"""
Taking an issuance into the database, and recording manual stock movements.

The two golden rules:
  1. Deduplication by Message-ID — an email is never counted twice.
  2. Intake is all-or-nothing — an issuance with a single problematic line waits
     for review as a whole, because a partial intake silently produces wrong stock.
  3. Nothing that is already in the count is ever taken back out of it — see
     `before_intake_cutoff`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import inventory, localtime, repo
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
    before_cutoff: bool = False
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


def before_intake_cutoff(email_date: datetime | None) -> datetime | None:
    """
    The intake cutoff that this email precedes, or None when it is inside the
    window.

    Why a cutoff exists at all: a reset to standard declares "the cupboard now
    holds exactly the standard quantity". Every issuance that physically
    happened before that moment is therefore already accounted for in what is
    on the shelf. An email from before it that has not yet been taken in would
    create a shortage for equipment that has already been replenished — the same
    issuance counted twice.

    Only issuances not yet applied are affected, and moving the date never takes
    one back out of the count. `remaining` is recomputed every time from the
    issuances applied *right now*, whereas a reset movement stores a fixed delta
    computed against the issuances applied *then*. Dropping an applied issuance
    would leave its reset movement behind as a credit against a shortage that no
    longer exists, and stock would climb above the standard with nothing on
    screen to say so. The way to correct stock that really is wrong is a stock
    count ("עדכון מלאי"), which records the difference as a movement of its own
    and so keeps the two ledgers in step.
    """
    cutoff = repo.get_intake_cutoff()
    if cutoff is None:
        return None
    return cutoff if _is_before(email_date, cutoff) else None


def _is_before(email_date: datetime | None, moment: datetime) -> bool:
    """
    An email date is compared as a moment, never as a string: `email_date` keeps
    the offset the email was sent with (+03:00 from Israel, +00:00 from a
    forward), so comparing the stored text would put 02:00 Israel time after a
    midnight-UTC boundary it actually precedes.
    """
    if email_date is None:
        return False
    return localtime.as_utc(email_date) < localtime.as_utc(moment)


def _cutoff_note(cutoff: datetime) -> str:
    return (
        f"המייל קודם לתאריך תחילת הקליטה ({localtime.format_dt(cutoff)}) ולכן אינו נספר במלאי — "
        "ההנפקה הזו כבר מגולמת באיפוס לתקן שבוצע אחריה.\n"
        "אם היא כן צריכה להיכנס — יש להזיז את התאריך בלוח המצב ולנתח מחדש."
    )


def _cutoff_message(cutoff: datetime) -> str:
    return f"המייל קודם לתאריך תחילת הקליטה ({localtime.format_dt(cutoff)}) ולא נקלט למלאי."


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

    if not parsed.looks_like_issuance:
        missing = (
            f'לא נמצא בו המשפט "{fmt.issuance_marker}" ואף לא רשימת מוצרים'
            if fmt.issuance_marker
            else "לא נמצאה בו רשימת מוצרים"
        )
        return (
            IGNORED,
            f"המייל אינו נראה כמו הודעת הנפקה — {missing}.",
            "המייל אינו הודעת הנפקה ולכן לא נקלט.",
            lines,
        )
    if not parsed.has_items_section:
        # An issuance whose item list could not be found — an issuance to an
        # organizational unit, or a changed template. It is not dropped: an
        # email that identified itself as an issuance always ends up in front
        # of a person, because an issuance quietly ignored is stock gone wrong.
        return (
            NEEDS_REVIEW,
            "\n".join([*notes, "המייל נראה כהודעת הנפקה אך לא נמצאה בו רשימת המוצרים — נדרש טיפול ידני."]),
            "המייל נראה כהודעת הנפקה אך לא ניתן היה לקרוא את רשימת המוצרים — ממתין לטיפול.",
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

    # An issuance already in the count is deliberately left out of this check:
    # the cutoff governs what may still enter stock, never what comes back out
    # of it — see before_intake_cutoff.
    if issuance.status != APPLIED and status != IGNORED:
        cutoff = before_intake_cutoff(issuance.email_date)
        if cutoff is not None:
            status, note, message = IGNORED, _cutoff_note(cutoff), _cutoff_message(cutoff)

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
    content_key = content_fingerprint(parsed) if parsed.lines else None

    # Before the cutoff the content of the email does not matter, so this
    # decision comes first — except over an email already ignored for a more
    # specific reason, whose own explanation is the more useful one to keep.
    stamp = email_date or datetime.now(timezone.utc)
    cutoff = None if status == IGNORED else before_intake_cutoff(stamp)
    if cutoff is not None:
        status, note, message = IGNORED, _cutoff_note(cutoff), _cutoff_message(cutoff)

    # An issuance that looks identical to one already applied is neither applied
    # nor discarded on its own — it goes to a manual decision, because the email
    # cannot tell us whether this is a re-forward or genuinely a second issuance
    # of the same equipment to the same person.
    if status == APPLIED:
        twin = repo.find_applied_with_content(content_key)
        if twin is not None:
            status = NEEDS_REVIEW
            note = _duplicate_note(twin)
            message = "המייל נראה כהעברה חוזרת של הנפקה שכבר נקלטה — ממתין להכרעה."

    issuance_id = repo.insert_issuance(
        message_id=message_id,
        email_date=stamp.isoformat(timespec="seconds"),
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
    return IngestResult(
        issuance_id=issuance_id,
        status=status,
        before_cutoff=cutoff is not None,
        message=message,
    )


def approve_issuance(issuance_id: int) -> tuple[bool, str]:
    """Approves an issuance that was waiting for review. Fails if a line is still unmatched."""
    issuance = repo.get_issuance(issuance_id)
    if issuance is None:
        return False, "ההנפקה לא נמצאה."
    if issuance.status == APPLIED:
        return False, "ההנפקה כבר נקלטה."
    cutoff = before_intake_cutoff(issuance.email_date)
    if cutoff is not None:
        return False, (
            f"ההנפקה קודמת לתאריך תחילת הקליטה ({localtime.format_dt(cutoff)}) ולכן אינה נכנסת "
            "למלאי — היא כבר מגולמת באיפוס לתקן שבוצע אחריה. אם היא כן צריכה להיכנס, "
            "יש להזיז את התאריך בלוח המצב."
        )
    if not issuance.lines:
        return False, "אין שורות פריטים בהנפקה הזו."
    unmatched = [line.raw_sku for line in issuance.lines if not line.matched]
    if unmatched:
        return False, "עדיין יש שורות בלי שיוך לפריט: " + ", ".join(unmatched)
    repo.set_issuance_status(issuance_id, APPLIED, None)
    return True, f"ההנפקה נקלטה — {len(issuance.lines)} פריטים."


def ignore_issuance(issuance_id: int, note: str = "סומנה ידנית להתעלמות.") -> None:
    repo.set_issuance_status(issuance_id, IGNORED, note)


#: A cap on the batch operations below. High enough that the mailbox of a
#: single equipment centre never reaches it, low enough to stay a bounded query.
_ALL_ISSUANCES = 5000


@dataclass
class CancelResult:
    cancelled: int = 0
    pending_closed: int = 0
    items_affected: int = 0
    #: Items left too high by the cancellation, as (sku, name, units). Their
    #: asserted quantity had already absorbed the issuance — see
    #: `cancel_double_counted`.
    needs_recount: list[tuple[str, str, int]] = field(default_factory=list)


def _double_counted(moment: datetime) -> list[repo.Issuance]:
    """
    The applied issuances that are being counted twice: their email predates
    `moment`, and they entered the database only after a stock movement had
    already settled the count.

    The second half is what makes this correct, and it is not the same as the
    email date. A stock count states "the shelf holds exactly this much now", so
    an issuance ingested *afterwards* that describes an earlier event subtracts
    from a quantity that already reflects it. An issuance that was in the
    database *before* that count is a different matter entirely: the count was
    computed with it included, so removing it now would push stock up by its
    quantity.

    With no movement ever recorded there is nothing that could have absorbed
    them, so the email date alone decides.
    """
    settled_at = repo.earliest_movement_at()
    return [
        issuance
        for issuance in repo.list_issuances((APPLIED,), limit=_ALL_ISSUANCES)
        if _is_before(issuance.email_date, moment)
        and (
            settled_at is None
            or (issuance.created_at is not None and issuance.created_at > settled_at)
        )
    ]


def cancel_double_counted(moment: datetime) -> CancelResult:
    """
    Takes the double-counted issuances out of the stock count, so that what is
    on the screen matches what is on the shelf.

    This is the retroactive half of the intake cutoff: the cutoff decides about
    emails still to arrive, this decides about the ones already in the database.

    Why not simply reset to standard instead: a reset forces the item to exactly
    its standard quantity, wiping out the shortage from issuances after the date
    too — those are real and still outstanding. This removes only the part of
    the count that was subtracted twice, and records no compensating movements
    at all.

    `needs_recount` carries the one case this cannot fix by itself. An item that
    had a stock movement recorded *after* the issuance entered has already
    absorbed it into an asserted quantity, so removing the issuance now leaves
    that item too high by its quantity. Those items are reported by name rather
    than quietly left wrong, and a stock count ("עדכון מלאי") on each of them
    puts it right.
    """
    moment = localtime.as_utc(moment)
    applied = _double_counted(moment)
    # An issuance from before the date can never be approved anyway, so leaving
    # it waiting in the review queue would only mislead.
    pending = [
        issuance
        for issuance in repo.list_issuances((NEEDS_REVIEW,), limit=_ALL_ISSUANCES)
        if _is_before(issuance.email_date, moment)
    ]
    if not applied and not pending:
        return CancelResult()

    units: dict[int, int] = {}
    names: dict[int, tuple[str, str]] = {}
    for issuance in applied:
        for line in issuance.lines:
            if line.item_id:
                units[line.item_id] = units.get(line.item_id, 0) + line.qty
                names[line.item_id] = (line.item_sku or line.raw_sku, line.item_name or line.raw_name)

    stamps = [issuance.created_at for issuance in applied if issuance.created_at]
    absorbed = repo.items_with_movements_since(list(units), min(stamps)) if stamps else set()

    note = (
        f"בוטלה הספירה — ההנפקה נקלטה למערכת אחרי שהמלאי כבר נספר, "
        f"ולכן נגרעה פעמיים. התאריך שנבחר: {localtime.format_dt(moment)}."
    )
    return CancelResult(
        cancelled=repo.set_issuances_status([i.id for i in applied], IGNORED, note),
        pending_closed=repo.set_issuances_status([i.id for i in pending], IGNORED, note),
        items_affected=len(units),
        needs_recount=sorted(
            ((names[i][0], names[i][1], units[i]) for i in absorbed), key=lambda r: -r[2]
        ),
    )


def count_double_counted(moment: datetime) -> int:
    """How many issuances `cancel_double_counted` would take out — for the confirmation."""
    return len(_double_counted(localtime.as_utc(moment)))


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
