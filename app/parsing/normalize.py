"""
SKU normalization.

Must run on both sides — on the SKU from the import file and on the SKU from
the email — otherwise the comparison fails on a stray space or a dash.
"""
from __future__ import annotations

import re

# Arabic-Indic digits (٠-٩) and the extended ones (۰-۹), plus fullwidth digits (０-９).
_DIGIT_TRANSLATION = {}
for _base in (0x0660, 0x06F0, 0xFF10):
    for _d in range(10):
        _DIGIT_TRANSLATION[_base + _d] = ord("0") + _d

# Characters that separate the parts of a SKU and carry no information: spaces,
# dashes (including the Hebrew maqaf and typographic dashes), dots, slashes,
# underscores, and invisible directional marks.
_STRIP_RE = re.compile(r"[\s\-‐-―־._/\\‎‏‪-‮⁦-⁩]+")


def normalize_sku(raw: str | None) -> str:
    """
    Converts a SKU into its canonical comparison form.

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
    """Collapses whitespace and strips directional marks — for item and people names."""
    if not raw:
        return ""
    text = re.sub(r"[‎‏‪-‮⁦-⁩]", "", str(raw))
    return re.sub(r"\s+", " ", text).strip()
