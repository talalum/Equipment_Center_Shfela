"""
Shared test infrastructure — a clean database for every test.

By default it runs against SQLite in a temporary file, with no external
dependency at all. If DATABASE_URL_TEST is set, those very same tests run
against a real Postgres:

    DATABASE_URL_TEST=postgresql://... py -m unittest discover -s tests -t .

That way one test suite verifies both engines, and there is no risk of the
behaviour diverging between them without anyone noticing.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "emails"
SAMPLE_EMAIL = (FIXTURES / "sample_issuance.txt").read_text(encoding="utf-8")
REAL_CSV = Path(__file__).resolve().parent.parent / "data" / "Inventory_Report.csv"

#: Address of the Postgres database used for testing. Empty = run against SQLite.
POSTGRES_TEST_URL = os.environ.get("DATABASE_URL_TEST", "")

#: The reverse of the dependency order, so that TRUNCATE does not trip over
#: foreign keys.
_ALL_TABLES = "issuance_lines, adjustments, import_runs, pending_imports, settings, issuances, items"


class DBTestCase(unittest.TestCase):
    """Every test gets an empty database, so nothing leaks between tests."""

    def setUp(self) -> None:
        from app import config, db

        # The default is a site with no password. A class testing authentication
        # sets that up for itself.
        self._auth = (config.APP_PASSWORD_HASH, config.SESSION_SECRET)
        config.APP_PASSWORD_HASH = ""
        config.SESSION_SECRET = "unit-test-secret"

        self._tmp = None
        self._prev_url = config.DATABASE_URL

        if POSTGRES_TEST_URL:
            config.DATABASE_URL = POSTGRES_TEST_URL
            db.reset_for_tests()
            db.init_db()
            # A fast wipe instead of rebuilding the schema for every test.
            db.connect().execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
        else:
            config.DATABASE_URL = ""
            self._tmp = tempfile.TemporaryDirectory()
            os.environ["DB_PATH"] = str(Path(self._tmp.name) / "test.db")
            config.DB_PATH = os.environ["DB_PATH"]
            db.reset_for_tests()
            db.init_db()

    def tearDown(self) -> None:
        from app import config, db

        config.APP_PASSWORD_HASH, config.SESSION_SECRET = self._auth
        db.reset_for_tests()
        config.DATABASE_URL = self._prev_url
        if self._tmp is not None:
            self._tmp.cleanup()

    def load_real_items(self) -> None:
        from app import importer

        importer.import_items(REAL_CSV.read_bytes(), REAL_CSV.name)


# --------------------------------------------------------------- xlsx fixture

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId7"/></sheets>
</workbook>"""

# Deliberately not sheet1.xml, and a relationship id that is not rId1: the
# reader has to follow the relationship rather than guess the file name.
_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/theSheet.xml"/>
  <Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>"""


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def build_xlsx(header: list, rows: list) -> bytes:
    """
    A minimal but genuine xlsx, built the way Excel builds one: text through the
    shared-strings table, numbers stored as floats. A None cell is left out of
    the sheet entirely, exactly as Excel leaves out an empty cell.
    """
    shared: list[str] = []

    def cell(reference: str, value) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Excel keeps every number as a float — "1101.0", not "1101".
            return f'<c r="{reference}"><v>{float(value)}</v></c>'
        text = str(value)
        if text not in shared:
            shared.append(text)
        return f'<c r="{reference}" t="s"><v>{shared.index(text)}</v></c>'

    body = []
    for row_number, values in enumerate([header] + rows, start=1):
        cells = "".join(cell(f"{_column_name(i)}{row_number}", v) for i, v in enumerate(values))
        body.append(f'<row r="{row_number}">{cells}</row>')

    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>" + "".join(body) + "</sheetData></worksheet>"
    )
    strings = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}">'
        + "".join(f"<si><t>{t}</t></si>" for t in shared)
        + "</sst>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/sharedStrings.xml", strings)
        archive.writestr("xl/worksheets/theSheet.xml", sheet)
    return buffer.getvalue()
