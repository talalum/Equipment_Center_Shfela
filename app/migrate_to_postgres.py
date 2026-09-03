"""
העברת הנתונים מקובץ SQLite מקומי אל מסד Postgres.

    py -m app.migrate_to_postgres              # העברה רגילה
    py -m app.migrate_to_postgres --dry-run    # רק מראה מה יעבור
    py -m app.migrate_to_postgres --replace    # מוחק את היעד תחילה

המקור הוא DB_PATH והיעד הוא DATABASE_URL — שניהם נקראים מ-.env.
הסקריפט קורא מהמקור בלבד-קריאה ולעולם לא משנה אותו, ולכן בטוח להריץ אותו
שוב אחרי תיקון.

המזהים המקוריים נשמרים כפי שהם, כדי שהקשרים בין הטבלאות יישארו תקינים.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from app import config, db
from app.console import force_utf8_output

#: סדר ההעברה הוא סדר התלויות: טבלה מועברת רק אחרי מה שהיא מצביעה אליו.
TABLES = ("items", "issuances", "issuance_lines", "adjustments", "import_runs")

BATCH = 500


def _open_source(path: str) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise SystemExit(f"לא נמצא קובץ מסד מקור: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _source_counts(src: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TABLES:
        try:
            counts[table] = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = 0  # טבלה שלא קיימת במסד ישן
    return counts


def _target_counts(target) -> dict[str, int]:
    return {
        table: int(target.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        for table in TABLES
    }


def _copy_table(src: sqlite3.Connection, target, table: str) -> int:
    """
    מעתיק טבלה אחת. העמודות נקראות מהמקור בזמן ריצה, ולכן מסד שנוצר לפני
    שנוספה עמודה יעבור בלי לערוך כאן דבר.
    """
    try:
        cursor = src.execute(f"SELECT * FROM {table}")
    except sqlite3.OperationalError:
        return 0

    columns = [d[0] for d in cursor.description]
    column_list = ", ".join(columns)
    placeholders = ", ".join("?" * len(columns))
    sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"

    moved = 0
    while rows := cursor.fetchmany(BATCH):
        target.executemany(sql, [tuple(row[c] for c in columns) for row in rows])
        moved += len(rows)
    return moved


def _reset_sequences(target) -> None:
    """
    אחרי הכנסה של מזהים מפורשים המונה הפנימי עדיין עומד על 1, והכנסה
    הבאה הייתה מתנגשת. מיישרים אותו לערך הגבוה ביותר שקיים בפועל.
    """
    for table in TABLES:
        target.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 0) + 1,
                false
            )
            """
        )


def _truncate(target) -> None:
    target.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")


def main(argv: list[str]) -> int:
    force_utf8_output()
    dry_run = "--dry-run" in argv
    replace = "--replace" in argv

    if not config.DATABASE_URL:
        raise SystemExit(
            "DATABASE_URL לא מוגדר. יש להוסיף אותו ל-.env עם כתובת מסד ה-Postgres."
        )

    print("\n=== העברת נתונים ל-Postgres ===\n")
    print(f"מקור: {config.DB_PATH}")
    print(f"יעד:  {config.DATABASE_URL.split('@')[-1]}\n")  # בלי הסיסמה

    src = _open_source(config.DB_PATH)
    counts = _source_counts(src)

    print("במקור:")
    for table, n in counts.items():
        print(f"  {table:16} {n}")

    if dry_run:
        # בודקים את החיבור ליעד גם בהרצה יבשה: זו התקלה הנפוצה ביותר,
        # ועדיף לגלות אותה לפני שנוגעים בנתונים ולא באמצע ההעברה.
        print("\nבודק חיבור ליעד...")
        try:
            version = db.connect().execute("SELECT version() AS v").fetchone()["v"]
        except Exception as exc:
            raise SystemExit(f"  ✗ החיבור נכשל:\n     {exc}")
        print(f"  ✓ מחובר: {version.split(' on ')[0]}")
        print("\n--dry-run — לא הועברו נתונים.\n")
        return 0

    db.init_db()  # יוצר את הסכימה ביעד אם עוד אין
    target = db.connect()

    existing = _target_counts(target)
    if any(existing.values()):
        if not replace:
            print("\nביעד כבר יש נתונים:")
            for table, n in existing.items():
                if n:
                    print(f"  {table:16} {n}")
            raise SystemExit(
                "\nההעברה בוטלה כדי לא לערבב נתונים.\n"
                "להריץ שוב עם --replace כדי למחוק את היעד ולהעביר מחדש."
            )
        print("\n--replace — מוחק את הנתונים הקיימים ביעד.")
        _truncate(target)

    print("\nמעביר:")
    for table in TABLES:
        moved = _copy_table(src, target, table)
        print(f"  {table:16} {moved}")

    _reset_sequences(target)

    final = _target_counts(target)
    mismatched = [t for t in TABLES if final[t] != counts[t]]
    if mismatched:
        raise SystemExit(f"\nאי-התאמה בספירה בטבלאות: {mismatched}. ההעברה לא הושלמה כראוי.")

    print("\n" + "=" * 50)
    print("ההעברה הושלמה. הספירות ביעד תואמות למקור.")
    print("קובץ ה-SQLite לא שונה ונשאר כגיבוי.")
    print("=" * 50 + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nבוטל.")
        sys.exit(1)
