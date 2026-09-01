"""
פירוק מייל הנפקה לרשומות מסודרות.

עקרון: אף פעם לא מנחשים. שורה בין העוגנים שלא נפרסה מעבירה את *כל* ההנפקה
לביקורת ידנית — קליטה חלקית תיצור מלאי שגוי בשקט, וזה בדיוק מה שהמערכת
אמורה למנוע.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app import config
from app.parsing.normalize import clean_text, normalize_sku


@dataclass(frozen=True)
class ParsedLine:
    raw_name: str
    raw_sku: str
    qty: int

    @property
    def normalized_sku(self) -> str:
        return normalize_sku(self.raw_sku)


@dataclass
class ParsedIssuance:
    recipient: str | None = None
    issuer: str | None = None
    center: str | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: האם נמצאה בכלל כותרת רשימת הפריטים. מבדיל בין "מייל הנפקה שנכשל
    #: בפירוק" (דורש טיפול) לבין "מייל שאינו הנפקה" (אפשר להתעלם).
    has_items_section: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.lines)


@dataclass(frozen=True)
class EmailFormat:
    items_start: str
    items_end: str
    item_line: re.Pattern[str]
    issuer: re.Pattern[str]
    center: re.Pattern[str]
    recipient: re.Pattern[str]
    expected_center: str


@lru_cache(maxsize=4)
def load_format(path: str | None = None) -> EmailFormat:
    fmt_path = Path(path) if path else config.EMAIL_FORMAT_PATH
    raw = yaml.safe_load(fmt_path.read_text(encoding="utf-8"))
    return EmailFormat(
        items_start=raw["items_start"],
        items_end=raw["items_end"],
        item_line=re.compile(raw["item_line"]),
        issuer=re.compile(raw["issuer"]),
        center=re.compile(raw["center"]),
        recipient=re.compile(raw["recipient"]),
        expected_center=clean_text(raw.get("expected_center") or ""),
    )


# כשג'ימייל מציג מייל HTML כטקסט, הוא עוטף מודגשים בכוכביות:
#   *המוצרים שהונפקו*:      *מרכז ציוד: *מרכז ציוד שפלה
# אסור להסיר את הכוכבית מתוך שמות פריטים כמו "פד גזה סטרילי 10*10",
# ולכן מוסרות רק כוכביות שאינן בין שתי ספרות.
_EMPHASIS_RE = re.compile(r"(?<!\d)\*|\*(?!\d)")

# תחילית ציטוט בהעברת מייל (נפוץ ב-Outlook): "> שורה מקורית".
_QUOTE_RE = re.compile(r"^\s*(?:>\s?)+")


def _clean_line(line: str) -> str:
    return _EMPHASIS_RE.sub("", _QUOTE_RE.sub("", line.rstrip()))


def _find_anchor(lines: list[str], anchor: str, start: int = 0) -> int:
    for idx in range(start, len(lines)):
        if anchor in lines[idx]:
            return idx
    return -1


def _first_match(lines: list[str], pattern: re.Pattern[str], group: str) -> str | None:
    for line in lines:
        m = pattern.match(line)
        if m:
            return clean_text(m.group(group))
    return None


def parse(raw_text: str, fmt: EmailFormat | None = None) -> ParsedIssuance:
    """מפרק את גוף המייל. לא נוגע במסד הנתונים ולא מחליט מה ייקלט."""
    fmt = fmt or load_format()
    result = ParsedIssuance()

    if not raw_text or not raw_text.strip():
        result.errors.append("גוף המייל ריק.")
        return result

    normalised = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean_line(line) for line in normalised.split("\n")]

    result.issuer = _first_match(lines, fmt.issuer, "issuer")
    result.center = _first_match(lines, fmt.center, "center")
    result.recipient = _first_match(lines, fmt.recipient, "recipient")

    start = _find_anchor(lines, fmt.items_start)
    if start == -1:
        result.errors.append(f'לא נמצא העוגן "{fmt.items_start}" — ייתכן שתבנית המייל השתנתה.')
        return result
    result.has_items_section = True

    end = _find_anchor(lines, fmt.items_end, start + 1)
    if end == -1:
        result.errors.append(f'לא נמצא העוגן "{fmt.items_end}" — ייתכן שהמייל נחתך.')
        return result

    seen: dict[str, int] = {}
    for line in lines[start + 1 : end]:
        if not line.strip():
            continue
        m = fmt.item_line.match(line.strip())
        if not m:
            result.errors.append(f"שורה לא מזוהה: {line.strip()}")
            continue
        qty = int(m.group("qty"))
        if qty <= 0:
            result.errors.append(f"כמות לא חוקית ({qty}) בשורה: {line.strip()}")
            continue
        parsed = ParsedLine(
            raw_name=clean_text(m.group("name")),
            raw_sku=clean_text(m.group("sku")),
            qty=qty,
        )
        # אותו מק"ט פעמיים באותו מייל — מאחדים ולא מאבדים כמות.
        key = parsed.normalized_sku
        if key in seen:
            idx = seen[key]
            merged = result.lines[idx]
            result.lines[idx] = ParsedLine(merged.raw_name, merged.raw_sku, merged.qty + parsed.qty)
        else:
            seen[key] = len(result.lines)
            result.lines.append(parsed)

    if not result.lines and not result.errors:
        result.errors.append("לא נמצאו שורות פריטים בין העוגנים.")

    return result


def center_matches(parsed: ParsedIssuance, fmt: EmailFormat | None = None) -> bool:
    """האם ההנפקה שייכת למרכז הציוד שלנו. מרכז ריק בהגדרות = לקלוט מכולם."""
    fmt = fmt or load_format()
    if not fmt.expected_center:
        return True
    return clean_text(parsed.center) == fmt.expected_center
