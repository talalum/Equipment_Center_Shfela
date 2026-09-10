"""Import of the standard-quantity file — against the real file."""
from __future__ import annotations

import unittest

from tests.base import REAL_CSV, DBTestCase, build_xlsx

from app import importer, inventory, repo
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

    def test_the_startup_import_leaves_the_stock_alone(self) -> None:
        """
        A fresh installation starts full at the standard quantity. The stock
        column of the file is only imported when a person uploads it and
        approves the comparison — never by the automatic import at startup.
        Here that means the plasters' 589 in stock must not show up anywhere:
        their standard is 320, and that is what they must start at.
        """
        importer.import_items(REAL_CSV.read_bytes())
        plaster = repo.find_item_by_sku("1102")
        self.assertEqual(plaster.standard_qty, 320)
        self.assertEqual(inventory.status_for_item(plaster).remaining, 320)
        # And the stock has no column of its own either — it is derived from
        # the movements, which is what keeps the history intact.
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


class ScanReadsBothFormats(DBTestCase):
    """scan() validates the file and touches nothing in the database."""

    HEADER = ['מק"ט', "שם פריט", "תקן מאושר", "מלאי לאחר הגעת משלוח "]

    def test_reads_an_excel_file(self) -> None:
        data = build_xlsx(self.HEADER, [[1101, "חסם עורקים", 19, 22], [1102, "פלסטר", 320, 325]])
        scanned = importer.scan(data, "מלאי.xlsx")
        self.assertEqual([r.sku for r in scanned.rows], ["1101", "1102"])
        self.assertEqual([r.stock for r in scanned.rows], [22, 325])
        self.assertTrue(scanned.has_stock)

    def test_reads_a_csv_with_the_older_column_names(self) -> None:
        csv = 'מק"ט,שם פריט,מלאי עדכני,תקן\n1101,חסם עורקים,22,19\n'
        scanned = importer.scan(csv, "old.csv")
        self.assertEqual(scanned.rows[0].standard, 19)
        self.assertEqual(scanned.rows[0].stock, 22)

    def test_the_post_delivery_column_wins_over_the_plain_one(self) -> None:
        """A file carrying both: the post-delivery figure is the later of the two."""
        csv = 'מק"ט,שם פריט,תקן מאושר,מלאי עדכני,מלאי לאחר הגעת משלוח\n1101,פריט,19,18,22\n'
        self.assertEqual(importer.scan(csv).rows[0].stock, 22)

    def test_a_file_without_a_stock_column_says_so(self) -> None:
        scanned = importer.scan('מק"ט,שם פריט,תקן\n1101,פריט,19\n')
        self.assertFalse(scanned.has_stock)
        self.assertIsNone(scanned.rows[0].stock)

    def test_an_empty_stock_cell_leaves_the_stock_alone(self) -> None:
        scanned = importer.scan(build_xlsx(self.HEADER, [[1101, "פריט", 19, None]]))
        self.assertTrue(scanned.has_stock)
        self.assertIsNone(scanned.rows[0].stock)

    def test_an_unreadable_stock_does_not_throw_the_row_away(self) -> None:
        scanned = importer.scan(build_xlsx(self.HEADER, [[1101, "פריט", 19, "הרבה"]]))
        self.assertEqual(len(scanned.rows), 1, "the item and its standard are still worth importing")
        self.assertIsNone(scanned.rows[0].stock)
        self.assertEqual(scanned.rejected, 0)
        self.assertTrue(any("מלאי לא מספרי" in p for p in scanned.problems))

    def test_a_negative_stock_is_not_imported(self) -> None:
        scanned = importer.scan(build_xlsx(self.HEADER, [[1101, "פריט", 19, -5]]))
        self.assertIsNone(scanned.rows[0].stock)
        self.assertTrue(any("מלאי שלילי" in p for p in scanned.problems))

    def test_a_corrupt_excel_file_is_reported_not_crashed_on(self) -> None:
        scanned = importer.scan(b"PK\x03\x04 broken", "broken.xlsx")
        self.assertFalse(scanned.usable)
        self.assertTrue(any("לא ניתן לקרוא" in p for p in scanned.problems))

    def test_scanning_writes_nothing(self) -> None:
        importer.scan(build_xlsx(self.HEADER, [[9001, "פריט חדש", 5, 5]]))
        self.assertIsNone(repo.find_item_by_sku("9001"))
        self.assertIsNone(repo.last_import_run())


class CompareAgainstTheSystem(DBTestCase):
    HEADER = ['מק"ט', "שם פריט", "תקן מאושר", "מלאי לאחר הגעת משלוח "]

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def compare(self, rows: list) -> importer.Comparison:
        return importer.compare(importer.scan(build_xlsx(self.HEADER, rows), "מלאי.xlsx"))

    def test_an_unchanged_row_is_not_listed_as_a_change(self) -> None:
        # 1101 starts at standard 19, so a file saying 19 in stock changes nothing.
        comparison = self.compare([[1101, "חסם עורקים", 19, 19]])
        self.assertEqual(comparison.changed_rows, [])
        self.assertEqual(comparison.unchanged_count, 1)

    def test_a_stock_change_carries_the_before_the_after_and_the_difference(self) -> None:
        comparison = self.compare([[1101, "חסם עורקים", 19, 25]])
        row = comparison.changed_rows[0]
        self.assertEqual((row.effective_before, row.stock, row.stock_delta), (19, 25, 6))
        self.assertTrue(row.stock_changed)
        self.assertFalse(row.standard_changed)

    def test_a_new_item_is_marked_as_new(self) -> None:
        row = self.compare([[9001, "פריט חדש", 5, 5]]).changed_rows[0]
        self.assertTrue(row.is_new)
        self.assertIsNone(row.standard_before)

    def test_a_rename_is_shown(self) -> None:
        row = self.compare([[1101, "חסם עורקים חדש", 19, 19]]).changed_rows[0]
        self.assertTrue(row.renamed)
        self.assertEqual(row.name_before, "חסם עורקים")

    def test_items_absent_from_the_file_are_listed(self) -> None:
        comparison = self.compare([[1101, "חסם עורקים", 19, 19]])
        self.assertEqual(len(comparison.missing), 75, "76 items in the system, one of them in the file")
        self.assertNotIn("1101", [i.sku for i in comparison.missing])

    def test_the_comparison_writes_nothing(self) -> None:
        self.compare([[1101, "חסם עורקים", 19, 999], [9001, "חדש", 5, 5]])
        self.assertIsNone(repo.find_item_by_sku("9001"))
        self.assertEqual(inventory.status_for_item(repo.find_item_by_sku("1101")).remaining, 19)


class ApplyTheStock(DBTestCase):
    HEADER = ['מק"ט', "שם פריט", "תקן מאושר", "מלאי לאחר הגעת משלוח "]

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def apply(self, rows: list, with_stock: bool = True) -> importer.ImportResult:
        return importer.apply(importer.scan(build_xlsx(self.HEADER, rows), "מלאי.xlsx"), with_stock=with_stock)

    def remaining(self, sku: str) -> int:
        return inventory.status_for_item(repo.find_item_by_sku(sku)).remaining

    def test_the_stock_lands_on_the_figure_in_the_file(self) -> None:
        self.apply([[1101, "חסם עורקים", 19, 25]])
        self.assertEqual(self.remaining("1101"), 25)

    def test_the_change_is_recorded_as_a_movement(self) -> None:
        self.apply([[1101, "חסם עורקים", 19, 25]])
        movement = repo.list_adjustments(limit=5)[0]
        self.assertEqual(movement.delta, 6)
        self.assertIn("מלאי.xlsx", movement.reason)

    def test_an_item_already_at_the_right_quantity_leaves_no_record(self) -> None:
        result = self.apply([[1101, "חסם עורקים", 19, 19]])
        self.assertEqual(result.stock_updated, 0)
        self.assertEqual(repo.list_adjustments(limit=5), [])

    def test_applying_the_same_file_twice_changes_nothing_the_second_time(self) -> None:
        self.apply([[1101, "חסם עורקים", 19, 25]])
        second = self.apply([[1101, "חסם עורקים", 19, 25]])
        self.assertEqual(second.stock_updated, 0)
        self.assertEqual(self.remaining("1101"), 25)

    def test_an_issuance_between_the_scan_and_the_approval_does_not_shift_the_result(self) -> None:
        """
        The file states what is in the cupboard. Whatever arrives while the
        comparison is on screen, the stock must end up at the figure in the file.
        """
        from tests.base import SAMPLE_EMAIL

        from app import ingest

        scanned = importer.scan(build_xlsx(self.HEADER, [[1111, "תחבושת אישית", 44, 30]]), "מלאי.xlsx")
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-mid-approval", source="paste")  # takes 2 off 1111
        self.assertEqual(self.remaining("1111"), 42)

        importer.apply(scanned)
        self.assertEqual(self.remaining("1111"), 30)

    def test_a_raised_standard_lifts_the_stock_with_it(self) -> None:
        """
        The stock is derived from the standard, so raising the standard raises
        it too. An import that raises both by the same amount is therefore not
        a movement — there is nothing left to correct.
        """
        result = self.apply([[1101, "חסם עורקים", 25, 25]])
        self.assertEqual(self.remaining("1101"), 25)
        self.assertEqual(result.stock_updated, 0)

    def test_without_the_stock_flag_only_names_and_standards_move(self) -> None:
        result = self.apply([[1101, "חסם עורקים", 19, 25]], with_stock=False)
        self.assertEqual(result.stock_updated, 0)
        self.assertEqual(self.remaining("1101"), 19)

    def test_a_row_with_no_stock_in_the_file_is_left_alone(self) -> None:
        self.apply([[1101, "חסם עורקים", 19, None]])
        self.assertEqual(self.remaining("1101"), 19)
        self.assertEqual(repo.list_adjustments(limit=5), [])

    def test_a_new_item_gets_its_stock_too(self) -> None:
        self.apply([[9001, "פריט חדש", 10, 3]])
        self.assertEqual(self.remaining("9001"), 3)

    def test_the_run_is_recorded(self) -> None:
        self.apply([[1101, "חסם עורקים", 19, 25]])
        self.assertEqual(repo.last_import_run().filename, "מלאי.xlsx")


class PendingPayload(DBTestCase):
    """The scan survives the wait between the upload and the approval."""

    def test_a_scan_round_trips_through_storage(self) -> None:
        scanned = importer.scan('מק"ט,שם פריט,תקן,מלאי עדכני\n1101,פריט,19,22\n', "x.csv")
        restored = importer.from_payload("x.csv", importer.to_payload(scanned))
        self.assertEqual(restored.rows, scanned.rows)
        self.assertEqual(restored.has_stock, scanned.has_stock)

    def test_a_row_stored_by_an_older_version_is_dropped_not_crashed_on(self) -> None:
        payload = {"rows": [{"sku": "1101"}, {"sku": "1102", "name": "פריט", "standard": 5, "stock": None}]}
        restored = importer.from_payload("x.csv", payload)
        self.assertEqual([r.sku for r in restored.rows], ["1102"])


if __name__ == "__main__":
    unittest.main()
