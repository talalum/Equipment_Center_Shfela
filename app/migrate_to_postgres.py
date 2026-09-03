"""
Moving the data from a local SQLite file into a Postgres database.

    py -m app.migrate_to_postgres              # an ordinary migration
    py -m app.migrate_to_postgres --dry-run    # only shows what would move
    py -m app.migrate_to_postgres --replace    # wipes the target first

The source is DB_PATH and the target is DATABASE_URL — both are read from .env.
The script opens the source read-only and never modifies it, so it is safe to
run again after a fix.

The original ids are preserved as they are, so that the relations between the
tables stay intact.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from app import config, db
from app.console import force_utf8_output

#: The migration order is the dependency order: a table moves only after
#: whatever it points at.
TABLES = ("items", "issuances", "issuance_lines", "adjustments", "import_runs")

BATCH = 500


def _open_source(path: str) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise SystemExit(f"Source database file not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _source_counts(src: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TABLES:
        try:
            counts[table] = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = 0  # a table that does not exist in an older database
    return counts


def _target_counts(target) -> dict[str, int]:
    return {
        table: int(target.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        for table in TABLES
    }


def _copy_table(src: sqlite3.Connection, target, table: str) -> int:
    """
    Copies a single table. The columns are read from the source at runtime, so a
    database created before a column was added migrates without editing anything
    here.
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
    After inserting explicit ids the internal counter still stands at 1, and the
    next insert would collide. It is realigned to the highest id actually present.
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
            "DATABASE_URL is not set. Add it to .env with the address of the Postgres database."
        )

    print("\n=== Migrating data to Postgres ===\n")
    print(f"Source: {config.DB_PATH}")
    print(f"Target: {config.DATABASE_URL.split('@')[-1]}\n")  # without the password

    src = _open_source(config.DB_PATH)
    counts = _source_counts(src)

    print("In the source:")
    for table, n in counts.items():
        print(f"  {table:16} {n}")

    if dry_run:
        # The connection to the target is checked on a dry run too: it is by far
        # the most common failure, and it is better to find it before touching
        # any data rather than halfway through the migration.
        print("\nChecking the connection to the target...")
        try:
            version = db.connect().execute("SELECT version() AS v").fetchone()["v"]
        except Exception as exc:
            raise SystemExit(f"  ✗ The connection failed:\n     {exc}")
        print(f"  ✓ Connected: {version.split(' on ')[0]}")
        print("\n--dry-run — no data was moved.\n")
        return 0

    db.init_db()  # creates the schema on the target if it is not there yet
    target = db.connect()

    existing = _target_counts(target)
    if any(existing.values()):
        if not replace:
            print("\nThe target already holds data:")
            for table, n in existing.items():
                if n:
                    print(f"  {table:16} {n}")
            raise SystemExit(
                "\nThe migration was cancelled so as not to mix data together.\n"
                "Run again with --replace to wipe the target and migrate afresh."
            )
        print("\n--replace — wiping the existing data on the target.")
        _truncate(target)

    print("\nMigrating:")
    for table in TABLES:
        moved = _copy_table(src, target, table)
        print(f"  {table:16} {moved}")

    _reset_sequences(target)

    final = _target_counts(target)
    mismatched = [t for t in TABLES if final[t] != counts[t]]
    if mismatched:
        raise SystemExit(f"\nCount mismatch in tables: {mismatched}. The migration did not complete properly.")

    print("\n" + "=" * 50)
    print("The migration is complete. The counts on the target match the source.")
    print("The SQLite file was not modified and remains as a backup.")
    print("=" * 50 + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
