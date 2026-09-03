"""
פלט טקסט בעברית לטרמינל.

כל הודעות המערכת בעברית. בווינדוס, כשהפלט מנותב לקובץ או ל-pipe, פייתון
בוחר את הקידוד המקומי (cp1252/cp1255) ושורה בעברית מפילה את התהליך כולו
ב-UnicodeEncodeError. כאן מכריחים UTF-8, ולכן כל כלי שמדפיס עברית צריך
לקרוא ל-force_utf8_output() בתחילת הריצה.
"""
from __future__ import annotations

import sys


def force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # לא כל stream תומך — לא סיבה להיכשל
