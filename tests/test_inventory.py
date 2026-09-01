"""חישוב המלאי, שני כפתורי התנועה, והדדופליקציה."""
from __future__ import annotations

import unittest

from tests.base import SAMPLE_EMAIL, DBTestCase

from app import ingest, inventory, repo
from app.parsing.normalize import normalize_sku


class NormalizeSku(unittest.TestCase):
    def test_variants_collapse_to_one_key(self) -> None:
        for variant in ("1111", " 1111 ", "11-11", "11.11", "١١١١", "۱۱۱۱"):
            self.assertEqual(normalize_sku(variant), "1111", f"נכשל על {variant!r}")

    def test_empty_values(self) -> None:
        self.assertEqual(normalize_sku(""), "")
        self.assertEqual(normalize_sku(None), "")


class Matching(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def test_sku_variants_find_the_same_item(self) -> None:
        for variant in ("1111", " 1111 ", "11-11"):
            self.assertIsNotNone(repo.find_item_by_sku(variant), f"נכשל על {variant!r}")

    def test_unknown_sku_returns_none(self) -> None:
        self.assertIsNone(repo.find_item_by_sku("9999"))

    def test_matching_ignores_item_names(self) -> None:
        """מק\"ט 1102 נקרא 'פלסטרים' במייל ו'פלסטר' בקובץ — והשיוך עדיין נכון."""
        self.assertEqual(repo.find_item_by_sku("1102").name, "פלסטר")


class InventoryMath(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.item = repo.find_item_by_sku("1111")  # תקן 44

    def status(self) -> inventory.ItemStatus:
        return inventory.status_for_item(repo.get_item(self.item.id))

    def test_starts_full_at_standard(self) -> None:
        s = self.status()
        self.assertEqual((s.remaining, s.shortage), (44, 0))

    def test_issuance_creates_shortage(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        s = self.status()
        self.assertEqual((s.issued, s.remaining, s.shortage), (2, 42, 2))

    def test_reset_closes_the_shortage(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        self.assertEqual(ingest.record_reset(self.item), 2)
        s = self.status()
        self.assertEqual((s.remaining, s.shortage), (44, 0))

    def test_reset_is_idempotent(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        ingest.record_reset(self.item)
        self.assertIsNone(ingest.record_reset(self.item), "איפוס שני לא אמור לרשום תנועה")

    def test_edit_takes_the_counted_quantity_not_a_delta(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")  # נשאר 42
        self.assertEqual(ingest.record_edit(self.item, 40, "ספירת מלאי"), -2)
        s = self.status()
        self.assertEqual((s.remaining, s.shortage), (40, 4))

    def test_stock_may_exceed_the_standard(self) -> None:
        self.assertEqual(ingest.record_edit(self.item, 54, "אספקה גדולה"), 10)
        s = self.status()
        self.assertEqual((s.remaining, s.shortage), (54, 0))

    def test_reset_lowers_stock_when_above_standard(self) -> None:
        ingest.record_edit(self.item, 54, "אספקה גדולה")
        self.assertEqual(ingest.record_reset(self.item), -10)
        self.assertEqual(self.status().remaining, 44)

    def test_edit_to_same_value_records_nothing(self) -> None:
        self.assertIsNone(ingest.record_edit(self.item, 44, "ספירה"))
        self.assertEqual(len(repo.list_adjustments()), 0)

    def test_history_is_never_overwritten(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        ingest.record_edit(self.item, 40, "ספירת מלאי")
        ingest.record_reset(self.item)
        movements = [a for a in repo.list_adjustments() if a.item_id == self.item.id]
        self.assertEqual(len(movements), 2)
        self.assertEqual({m.kind for m in movements}, {"edit", "reset"})
        self.assertTrue(all(m.reason for m in movements))
        self.assertTrue(all(m.created_at for m in movements))


class AppliedEmailEffects(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def shortages(self) -> dict[str, int]:
        return {
            s.item.sku: s.shortage
            for s in inventory.status_for_all(repo.list_items())
            if s.in_shortage
        }

    def test_seven_items_go_short_with_expected_amounts(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        self.assertEqual(
            self.shortages(),
            {"1111": 2, "3702": 1, "1102": 1, "1108": 1, "1103": 1, "1104": 5, "1105": 5},
        )

    def test_same_message_id_counted_once(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        again = ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        self.assertTrue(again.duplicate)
        self.assertEqual(self.shortages()["1111"], 2, "מייל כפול לא אמור להיספר פעמיים")

    def test_pasting_identical_text_twice_is_deduplicated(self) -> None:
        mid = ingest.synthetic_message_id(SAMPLE_EMAIL)
        ingest.ingest_issuance(SAMPLE_EMAIL, mid, source="paste")
        self.assertTrue(ingest.ingest_issuance(SAMPLE_EMAIL, mid, source="paste").duplicate)
        self.assertEqual(self.shortages()["1111"], 2)

    def test_other_center_is_ignored_and_does_not_touch_stock(self) -> None:
        other = SAMPLE_EMAIL.replace("מרכז ציוד שפלה", "מרכז ציוד ירושלים")
        result = ingest.ingest_issuance(other, "m-other", source="paste")
        self.assertEqual(result.status, ingest.IGNORED)
        self.assertEqual(self.shortages(), {})

    def test_unknown_sku_holds_the_whole_issuance(self) -> None:
        broken = SAMPLE_EMAIL.replace("פלסטרים, מקט: 1102 - כמות: 1", "מזרן ואקום, מקט: 9999 - כמות: 1")
        result = ingest.ingest_issuance(broken, "m-unknown", source="paste")
        self.assertEqual(result.status, ingest.NEEDS_REVIEW)
        self.assertEqual(self.shortages(), {}, "גם השורות התקינות לא נכנסות עד לאישור")

    def test_unparsable_line_holds_the_whole_issuance(self) -> None:
        broken = SAMPLE_EMAIL.replace("פלסטרים, מקט: 1102 - כמות: 1", "טקסט חופשי")
        result = ingest.ingest_issuance(broken, "m-broken", source="paste")
        self.assertEqual(result.status, ingest.NEEDS_REVIEW)
        self.assertEqual(self.shortages(), {})


class ReviewFlow(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        broken = SAMPLE_EMAIL.replace("פלסטרים, מקט: 1102 - כמות: 1", "מזרן ואקום, מקט: 9999 - כמות: 1")
        self.issuance_id = ingest.ingest_issuance(broken, "m-review", source="paste").issuance_id

    def test_cannot_approve_while_a_line_is_unmatched(self) -> None:
        ok, message = ingest.approve_issuance(self.issuance_id)
        self.assertFalse(ok)
        self.assertIn("9999", message)

    def test_approve_after_assigning_the_missing_item(self) -> None:
        new_id = repo.create_item("9999", "מזרן ואקום", 10)
        line = next(l for l in repo.get_issuance(self.issuance_id).lines if not l.matched)
        repo.assign_line_item(line.id, new_id)

        ok, _ = ingest.approve_issuance(self.issuance_id)
        self.assertTrue(ok)
        self.assertEqual(inventory.status_for_item(repo.get_item(new_id)).shortage, 1)
        self.assertEqual(inventory.status_for_item(repo.find_item_by_sku("1111")).shortage, 2)

    def test_ignored_issuance_never_counts(self) -> None:
        ingest.ignore_issuance(self.issuance_id)
        self.assertEqual(inventory.status_for_item(repo.find_item_by_sku("1111")).shortage, 0)


class BulkReset(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")

    def test_resets_only_items_in_shortage(self) -> None:
        self.assertEqual(ingest.reset_all_shortages(), 7)
        remaining = [s for s in inventory.status_for_all(repo.list_items()) if s.in_shortage]
        self.assertEqual(remaining, [])
        self.assertEqual(len(repo.list_adjustments()), 7, "רק 7 הפריטים בחוסר נגעו")

    def test_no_shortages_means_no_movements(self) -> None:
        ingest.reset_all_shortages()
        self.assertEqual(ingest.reset_all_shortages(), 0)


class Sorting(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")

    def test_default_sort_puts_shortages_first(self) -> None:
        ordered = inventory.sort_statuses(inventory.status_for_all(repo.list_items()))
        self.assertEqual([s.item.sku for s in ordered[:2]], ["1104", "1105"])
        self.assertEqual([s.shortage for s in ordered[:3]], [5, 5, 2])

    def test_sort_is_stable_across_calls(self) -> None:
        first = [s.item.sku for s in inventory.sort_statuses(inventory.status_for_all(repo.list_items()))]
        second = [s.item.sku for s in inventory.sort_statuses(inventory.status_for_all(repo.list_items()))]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
