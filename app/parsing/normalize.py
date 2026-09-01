"""
נרמול מק"ט.

חייב לרוץ על שני הצדדים — גם על המק"ט מקובץ הייבוא וגם על המק"ט מהמייל —
אחרת ההשוואה תיכשל על רווח מיותר או מקף.
"""
from __future__ import annotations

import re

# ספרות ערביות-מזרחיות (٠-٩) והמורחבות (۰-۹), וכן ספרות רוחב-מלא (０-９).
_DIGIT_TRANSLATION = {}
for _base in (0x0660, 0x06F0, 0xFF10):
    for _d in range(10):
        _DIGIT_TRANSLATION[_base + _d] = ord("0") + _d

# תווים שמפרידים בין חלקי מק"ט ואינם נושאים מידע: רווחים, מקפים (כולל מקף עברי
# ומקפים טיפוגרפיים), נקודות, לוכסנים, קווים תחתונים ותווי כיווניות בלתי נראים.
_STRIP_RE = re.compile(r"[\s\-‐-―־._/\\‎‏‪-‮⁦-⁩]+")


def normalize_sku(raw: str | None) -> str:
    """
    ממיר מק"ט לצורת ההשוואה הקנונית.

    >>> normalize_sku(" 11-11 ")
    '1111'
    >>> normalize_sku("١١١١")
    '1111'
    """
    if not raw:
        return ""
    text = str(raw).translate(_DIGIT_TRANSLATION)
    text = _STRIP_RE.sub("", text)
    return text.upper()


def clean_text(raw: str | None) -> str:
    """מכווץ רווחים ומסיר תווי כיווניות — לשמות פריטים ולשמות אנשים."""
    if not raw:
        return ""
    text = re.sub(r"[‎‏‪-‮⁦-⁩]", "", str(raw))
    return re.sub(r"\s+", " ", text).strip()
