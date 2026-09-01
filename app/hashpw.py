"""
כלי עזר ליצירת ה-hash של סיסמת הכניסה.

    python -m app.hashpw

מדביקים את הפלט לתוך APP_PASSWORD_HASH בסביבת ההרצה.
"""
from __future__ import annotations

import getpass
import sys

from app.auth import hash_password


def main() -> int:
    password = getpass.getpass("סיסמה חדשה: ")
    if len(password) < 8:
        print("הסיסמה קצרה מדי — לפחות 8 תווים.", file=sys.stderr)
        return 1
    if password != getpass.getpass("שוב, לאימות: "):
        print("הסיסמאות אינן תואמות.", file=sys.stderr)
        return 1
    print("\nהוסיפי את השורה הזו למשתני הסביבה:\n")
    print(f"APP_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
