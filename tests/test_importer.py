"""Import of the standard-quantity file — against the real file."""
from __future__ import annotations

import unittest

from tests.base import REAL_CSV, DBTestCase

from app import importer, repo
from app.db import _existing_columns, connect

HEADER = 'מק"ט,שם פריט,מלאי עדכני,תקן,כמות חוסר,כמות להזמנה,הסבר\n'


class ImportRealFile(DBTestCase):
    def test_imports_all_76_items(self) -> None:
        result = importer.import_items(REAL_CSV.read_bytes(), "Inventory_Report.csv")
        self.assertEqual(result.created, 76)
        self.assertEqual(result.rejected, 0)
        self.assertEqual(len(repo.list_items()), 76)

    def test_total_standard_matches_source_file(self) -> None:
        importer.import_items(REAL_CSV.read_bytes())
        self.assertEqual(sum(i.standard_qty for i in repo.list_items()), 10_093)

    def test_current_stock_column_is_not_stored_anywhere(self) -> None:
        """
        The 'current stock' column is ignored on purpose. This test verifies that
        the value 589 (the plasters) did not leak into any column — their standard
        quantity is 320.
        """
        importer.import_items(REAL_CSV.read_bytes())
        plaster = repo.find_item_by_sku("1102")
        self.assertEqual(plaster.standard_qty, 320)
        columns = _existing_columns(connect(), "items")
        self.assertNotIn("current_stock", columns)
        self.assertNotIn("opening_stock", columns)

    def test_reimport_updates_and_does_not_duplicate(self) -> None:
        importer.import_items(REAL_CSV.read_bytes())
        again = importer.import_items(REAL_CSV.read_bytes())
        self.assertEqual(again.created, 0)
        self.assertEqual(again.updated, 76)
        self.assertEqual(len(repo.list_items()), 76)

    def test_reimport_keeps_issuance_history(self) -> None:
        from app import ingest, inventory
        from tests.base import SAMPLE_EMAIL

        importer.import_items(REAL_CSV.read_bytes())
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        importer.import_items(REAL_CSV.read_bytes())
        status = inventory.status_for_item(repo.find_item_by_sku("1111"))
        self.assertEqual(status.issued, 2, "a repeat import must not wipe the issuance history")


class ImportValidation(DBTestCase):
    def test_duplicate_sku_is_rejected_not_silently_overwritten(self) -> None:
        csv = HEADER + "1111,תחבושת אישית,58,44,0,0,\n1111,כפילות,1,99,0,0,\n"
        result = importer.import_items(csv)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.rejected, 1)
        self.assertTrue(any("מופיע כבר בשורה" in p for p in result.problems))
        self.assertEqual(repo.find_item_by_sku("1111").standard_qty, 44)

    def test_non_numeric_standard_is_rejected(self) -> None:
        result = importer.import_items(HEADER + "1200,פריט,0,הרבה,0,0,\n")
        self.assertEqual(result.rejected, 1)
        self.assertTrue(any("תקן לא מספרי" in p for p in result.problems))

    def test_missing_required_column(self) -> None:
        result = importer.import_items("שם פריט,תקן\nפריט,5\n")
        self.assertEqual(result.total_ok, 0)
        self.assertTrue(any("חסרות עמודות חובה" in p for p in result.problems))

    def test_blank_sku_rejected(self) -> None:
        result = importer.import_items(HEADER + " ,פריט,0,5,0,0,\n")
        self.assertEqual(result.rejected, 1)

    def test_bom_is_handled(self) -> None:
        result = importer.import_items(("﻿" + HEADER + "1300,פריט,0,7,0,0,\n").encode("utf-8"))
        self.assertEqual(result.created, 1)
        self.assertEqual(repo.find_item_by_sku("1300").standard_qty, 7)


if __name__ == "__main__":
    unittest.main()
