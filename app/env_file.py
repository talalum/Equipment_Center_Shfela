"""
טעינת קובץ .env.

בלי זה היה צריך להגדיר משתני סביבה מחדש בכל הפעלה — מייגע במיוחד בווינדוס.
מימוש קטן על הספרייה הסטנדרטית, בלי תלות חיצונית.
"""
from __future__ import annotations

import os
from pathlib import Path


def parse(text: str) -> dict[str, str]:
    """
    פורמט KEY=VALUE, שורה לשורה.

    נתמכים: שורות ריקות, הערות עם #, מרכאות מסביב לערך, ותחילית export.
    ערך יכול להכיל = (למשל hash של סיסמה), ולכן מפצלים רק על ה-= הראשון.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load(path: str | Path, override: bool = False) -> dict[str, str]:
    """
    טוען את הקובץ אל תוך משתני הסביבה.

    כברירת מחדל משתנה שכבר מוגדר בסביבה *מנצח* את הקובץ, כדי שבענן
    ההגדרות של שירות האירוח יגברו על קובץ שנשאר בטעות בתמונה.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}

    loaded = parse(text)
    for key, value in loaded.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded
