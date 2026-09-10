"""Reading an Excel sheet with the standard library — app/spreadsheet.py."""
from __future__ import annotations

import io
import unittest
import zipfile

from tests.base import build_xlsx

from app import spreadsheet

HEADER = ['מק"ט', "שם פריט", "תקן מאושר", "מלאי לאחר הגעת משלוח "]


class ReadRows(unittest.TestCase):
    def test_reads_the_header_and_the_rows(self) -> None:
        rows = spreadsheet.read_rows(build_xlsx(HEADER, [[1101, "חסם עורקים", 19, 19]]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["שם פריט"], "חסם עורקים")

    def test_a_whole_number_loses_the_excel_decimal_point(self) -> None:
        """
        Excel stores 1101 as 1101.0. The .0 has to be gone before normalize_sku
        sees it — that function strips dots, and would read 1101.0 as 11010.
        """
        rows = spreadsheet.read_rows(build_xlsx(HEADER, [[1101, "פריט", 19, 19]]))
        self.assertEqual(rows[0]['מק"ט'], "1101")
        self.assertEqual(rows[0]["תקן מאושר"], "19")

    def test_a_trailing_space_in_a_column_name_is_stripped(self) -> None:
        # The real report carries exactly such a column name.
        rows = spreadsheet.read_rows(build_xlsx(HEADER, [[1101, "פריט", 19, 22]]))
        self.assertIn("מלאי לאחר הגעת משלוח", rows[0])
        self.assertEqual(rows[0]["מלאי לאחר הגעת משלוח"], "22")

    def test_an_empty_cell_comes_back_empty_not_missing(self) -> None:
        rows = spreadsheet.read_rows(build_xlsx(HEADER, [[1101, "פריט", 19, None]]))
        self.assertEqual(rows[0]["מלאי לאחר הגעת משלוח"], "")
        self.assertEqual(len(rows[0]), len(HEADER))

    def test_a_row_with_nothing_in_it_is_dropped(self) -> None:
        rows = spreadsheet.read_rows(build_xlsx(HEADER, [[1101, "פריט", 19, 19], [None, None, None, None]]))
        self.assertEqual(len(rows), 1)

    def test_the_sheet_is_found_through_the_relationship(self) -> None:
        """The fixture's sheet is not called sheet1.xml, on purpose."""
        data = build_xlsx(HEADER, [[1101, "פריט", 19, 19]])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertNotIn("xl/worksheets/sheet1.xml", archive.namelist())
        self.assertEqual(len(spreadsheet.read_rows(data)), 1)

    def test_a_file_that_is_not_a_zip_is_refused(self) -> None:
        with self.assertRaises(spreadsheet.SpreadsheetError):
            spreadsheet.read_rows(b'\xd0\xcf\x11\xe0 not a zip')

    def test_a_zip_that_is_not_a_workbook_is_refused(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "nothing to see")
        with self.assertRaises(spreadsheet.SpreadsheetError):
            spreadsheet.read_rows(buffer.getvalue())

    def test_looks_like_xlsx_recognises_the_zip_signature(self) -> None:
        self.assertTrue(spreadsheet.looks_like_xlsx(build_xlsx(HEADER, [])))
        self.assertFalse(spreadsheet.looks_like_xlsx('מק"ט,שם פריט\n1,a\n'.encode("utf-8")))


if __name__ == "__main__":
    unittest.main()
