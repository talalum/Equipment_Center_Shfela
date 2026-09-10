"""
The intake cutoff — the date from which an email still counts towards stock.

A reset to standard declares the cupboard full, so an issuance email from before
that moment describes equipment that has already been replenished; taking it in
would count the same issuance twice. That is what this date prevents.

What it must never do is take an issuance already in the count back out of it.
The reset movement stores a fixed delta, computed against the issuances applied
at the time, so removing one side of that subtraction leaves a credit with
nothing behind it and stock climbs above the standard. `NeverRetroactive` is the
guard on that.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.base import SAMPLE_EMAIL, DBTestCase
from tests.test_mail import FakeIMAP

from app import config, db, ingest, inventory, localtime, repo
from app.mail import fetcher

#: A fixed moment, so that no test here depends on the clock.
CUTOFF = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

#: The same email with an unknown SKU, so intake sends it to review instead of
#: applying it — the state an issuance must be in for approval to be tested.
NEEDS_REVIEW_EMAIL = SAMPLE_EMAIL.replace("מקט: 1111", "מקט: 9999")


class Storage(DBTestCase):
    def test_absent_by_default(self) -> None:
        self.assertIsNone(repo.get_intake_cutoff())

    def test_round_trip(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        self.assertEqual(repo.get_intake_cutoff(), CUTOFF)

    def test_saving_again_replaces_the_value(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        repo.set_intake_cutoff(CUTOFF + timedelta(days=1))
        self.assertEqual(repo.get_intake_cutoff(), CUTOFF + timedelta(days=1))

    def test_clearing_removes_it(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        repo.set_intake_cutoff(None)
        self.assertIsNone(repo.get_intake_cutoff())

    def test_a_moment_without_a_timezone_is_read_as_utc(self) -> None:
        """Everything is stored in UTC, so a naive value cannot mean anything else."""
        repo.set_intake_cutoff(datetime(2026, 9, 10, 12, 0))
        self.assertEqual(repo.get_intake_cutoff(), CUTOFF)

    def test_an_unknown_setting_is_none_rather_than_an_error(self) -> None:
        self.assertIsNone(repo.get_setting("no-such-setting"))


class LastReset(DBTestCase):
    """The date offered on the screen is the moment of the last reset to standard."""

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.item = repo.find_item_by_sku("1111")  # standard quantity 44

    def test_none_before_any_reset(self) -> None:
        self.assertIsNone(repo.last_reset_at())

    def test_a_stock_count_is_not_a_reset(self) -> None:
        ingest.record_edit(self.item, 40, "ספירה")
        self.assertIsNone(repo.last_reset_at())

    def test_the_reset_moment_is_reported(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        ingest.record_reset(self.item)
        self.assertGreaterEqual(repo.last_reset_at(), before)

    def test_the_most_recent_reset_wins(self) -> None:
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        ingest.record_reset(self.item)
        first = repo.last_reset_at()
        ingest.record_edit(self.item, 40, "ספירה")
        ingest.record_reset(self.item)
        self.assertGreaterEqual(repo.last_reset_at(), first)


class Intake(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.item = repo.find_item_by_sku("1111")  # standard quantity 44
        repo.set_intake_cutoff(CUTOFF)

    def status(self) -> inventory.ItemStatus:
        return inventory.status_for_item(repo.get_item(self.item.id))

    def ingest_at(self, moment: datetime, message_id: str = "m-1") -> ingest.IngestResult:
        return ingest.ingest_issuance(SAMPLE_EMAIL, message_id, email_date=moment, source="email")

    def test_an_earlier_email_is_kept_but_not_counted(self) -> None:
        result = self.ingest_at(CUTOFF - timedelta(hours=1))
        self.assertEqual(result.status, ingest.IGNORED)
        self.assertTrue(result.before_cutoff)
        self.assertIsNotNone(result.issuance_id, "the email is recorded, not discarded")
        self.assertEqual(self.status().remaining, 44)

    def test_the_recorded_reason_names_the_date(self) -> None:
        result = self.ingest_at(CUTOFF - timedelta(hours=1))
        note = repo.get_issuance(result.issuance_id).review_note
        self.assertIn(localtime.format_dt(CUTOFF), note)

    def test_a_later_email_is_counted(self) -> None:
        result = self.ingest_at(CUTOFF + timedelta(minutes=1))
        self.assertEqual(result.status, ingest.APPLIED)
        self.assertFalse(result.before_cutoff)
        self.assertEqual(self.status().remaining, 42)

    def test_the_cutoff_moment_itself_is_inside_the_window(self) -> None:
        self.assertEqual(self.ingest_at(CUTOFF).status, ingest.APPLIED)

    def test_the_comparison_is_by_the_moment_not_by_the_day(self) -> None:
        """Same calendar day, three hours earlier — the equipment is already back."""
        self.assertEqual(self.ingest_at(CUTOFF - timedelta(hours=3)).status, ingest.IGNORED)

    def test_with_no_cutoff_any_date_is_taken_in(self) -> None:
        repo.set_intake_cutoff(None)
        self.assertEqual(self.ingest_at(datetime(2020, 1, 1, tzinfo=timezone.utc)).status,
                         ingest.APPLIED)

    def test_a_pasted_email_is_stamped_now_and_so_passes(self) -> None:
        """The paste screen has no Date header — such an email counts as arriving now."""
        result = ingest.ingest_issuance(SAMPLE_EMAIL, "p-1", source="paste")
        self.assertEqual(result.status, ingest.APPLIED)

    def test_a_more_specific_reason_is_not_overwritten(self) -> None:
        """An email that is no issuance at all keeps its own explanation."""
        result = ingest.ingest_issuance(
            "שלום, סתם מייל.", "m-x", email_date=CUTOFF - timedelta(days=5), source="email"
        )
        self.assertEqual(result.status, ingest.IGNORED)
        self.assertFalse(result.before_cutoff)
        note = repo.get_issuance(result.issuance_id).review_note
        self.assertIn("אינו נראה כמו הודעת הנפקה", note)

    def test_an_earlier_email_does_not_wait_in_the_review_queue(self) -> None:
        """It could never be applied, so leaving it pending would only mislead."""
        result = ingest.ingest_issuance(
            NEEDS_REVIEW_EMAIL, "m-2", email_date=CUTOFF - timedelta(days=1), source="email"
        )
        self.assertEqual(result.status, ingest.IGNORED)
        self.assertEqual(repo.count_issuances(ingest.NEEDS_REVIEW), 0)


class Approval(DBTestCase):
    """An issuance that reached review before the date was set cannot be waved in."""

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.item = repo.find_item_by_sku("1111")
        self.pending = ingest.ingest_issuance(
            NEEDS_REVIEW_EMAIL, "m-1", email_date=CUTOFF - timedelta(days=1), source="email"
        )
        self.assertEqual(self.pending.status, ingest.NEEDS_REVIEW)

    def test_approval_is_refused_and_explains_why(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        ok, message = ingest.approve_issuance(self.pending.issuance_id)
        self.assertFalse(ok)
        self.assertIn(localtime.format_dt(CUTOFF), message)
        self.assertEqual(repo.get_issuance(self.pending.issuance_id).status, ingest.NEEDS_REVIEW)

    def test_moving_the_date_back_lets_it_in(self) -> None:
        """The refusal is a decision about a date, and the date can be changed."""
        repo.set_intake_cutoff(CUTOFF)
        self.assertFalse(ingest.approve_issuance(self.pending.issuance_id)[0])

        repo.set_intake_cutoff(CUTOFF - timedelta(days=30))
        unmatched = [line for line in repo.get_issuance(self.pending.issuance_id).lines
                     if not line.matched]
        repo.assign_line_item(unmatched[0].id, self.item.id)
        self.assertTrue(ingest.approve_issuance(self.pending.issuance_id)[0])


class NeverRetroactive(DBTestCase):
    """
    The rule that makes the whole thing safe: the date governs what may still
    enter stock, never what comes back out of it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.item = repo.find_item_by_sku("1111")  # standard quantity 44
        self.applied = ingest.ingest_issuance(
            SAMPLE_EMAIL, "m-1", email_date=CUTOFF - timedelta(days=2), source="email"
        )
        self.assertEqual(self.applied.status, ingest.APPLIED)

    def status(self) -> inventory.ItemStatus:
        return inventory.status_for_item(repo.get_item(self.item.id))

    def test_setting_the_date_leaves_the_count_alone(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        self.assertEqual(self.status().remaining, 42)

    def test_re_analysis_does_not_undo_an_applied_issuance(self) -> None:
        """
        The trap this guards: after the reset, dropping the issuance the reset
        was computed against would leave a +2 credit behind and show 46 of a
        standard of 44 — more stock than the cupboard ever held.
        """
        ingest.record_reset(self.item)
        self.assertEqual(self.status().remaining, 44)

        repo.set_intake_cutoff(CUTOFF)
        ingest.reanalyse_issuance(self.applied.issuance_id)

        self.assertEqual(repo.get_issuance(self.applied.issuance_id).status, ingest.APPLIED)
        self.assertEqual(self.status().remaining, 44, "stock must not climb above the standard")

    def test_re_analysis_moves_a_pending_earlier_issuance_out(self) -> None:
        """One not yet in the count, by contrast, is exactly what the date is for."""
        pending = ingest.ingest_issuance(
            NEEDS_REVIEW_EMAIL, "m-2", email_date=CUTOFF - timedelta(days=1), source="email"
        )
        repo.set_intake_cutoff(CUTOFF)
        ingest.reanalyse_issuance(pending.issuance_id)
        self.assertEqual(repo.get_issuance(pending.issuance_id).status, ingest.IGNORED)

    def test_the_bulk_re_analysis_is_safe_too(self) -> None:
        ingest.record_reset(self.item)
        repo.set_intake_cutoff(CUTOFF)
        ingest.reanalyse_unapplied()
        self.assertEqual(self.status().remaining, 44)


class CancelThePast(DBTestCase):
    """
    Taking the issuances from before the date out of the count — the retroactive
    half of the same idea.

    The point of it over a reset to standard: a reset forces the item to exactly
    its standard quantity, wiping out the shortage from issuances after the date
    as well. This removes only the part that belongs to the past.
    """

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.item = repo.find_item_by_sku("1111")  # standard quantity 44
        self.old = ingest.ingest_issuance(
            SAMPLE_EMAIL, "old-1", email_date=CUTOFF - timedelta(days=2), source="email"
        )
        self.assertEqual(self.old.status, ingest.APPLIED)

    def status(self) -> inventory.ItemStatus:
        return inventory.status_for_item(repo.get_item(self.item.id))

    def add_recent_issuance(self) -> ingest.IngestResult:
        """A second, genuinely outstanding issuance — dated after the cutoff."""
        recent = SAMPLE_EMAIL.replace("שלום עם הנצח", "שלום דני כהן")
        result = ingest.ingest_issuance(
            recent, "new-1", email_date=CUTOFF + timedelta(days=1), source="email"
        )
        self.assertEqual(result.status, ingest.APPLIED)
        return result

    def test_the_old_issuance_leaves_the_count(self) -> None:
        self.assertEqual(self.status().remaining, 42)
        result = ingest.cancel_double_counted(CUTOFF)
        self.assertEqual(result.cancelled, 1)
        self.assertEqual(result.items_affected, 7)
        self.assertEqual(result.needs_recount, [])
        self.assertEqual(self.status().remaining, 44)

    def test_a_later_shortage_is_left_standing(self) -> None:
        """The whole reason not to use a reset to standard here."""
        self.add_recent_issuance()
        self.assertEqual(self.status().remaining, 40)

        ingest.cancel_double_counted(CUTOFF)

        s = self.status()
        self.assertEqual((s.remaining, s.shortage), (42, 2), "only the old issuance comes out")

    def test_no_compensating_movements_are_recorded(self) -> None:
        ingest.cancel_double_counted(CUTOFF)
        self.assertEqual(repo.list_adjustments(), [])
        self.assertIsNone(repo.last_reset_at())

    def test_the_issuance_stays_in_the_log_and_explains_itself(self) -> None:
        ingest.cancel_double_counted(CUTOFF)
        issuance = repo.get_issuance(self.old.issuance_id)
        self.assertEqual(issuance.status, ingest.IGNORED)
        self.assertIn(localtime.format_dt(CUTOFF), issuance.review_note)

    def test_it_can_be_undone_from_the_log(self) -> None:
        """An issuance marked as not counted can be re-analysed back in."""
        ingest.cancel_double_counted(CUTOFF)
        ingest.reanalyse_issuance(self.old.issuance_id)
        self.assertEqual(repo.get_issuance(self.old.issuance_id).status, ingest.APPLIED)
        self.assertEqual(self.status().remaining, 42)

    def test_undoing_it_needs_the_date_moved_first(self) -> None:
        """
        The realistic case: the date is saved as well, and re-analysis honours
        it — so bringing an issuance back means moving the date first. Said out
        loud in the confirmation, because otherwise it looks like the button did
        not work.
        """
        repo.set_intake_cutoff(CUTOFF)
        ingest.cancel_double_counted(CUTOFF)

        ingest.reanalyse_issuance(self.old.issuance_id)
        self.assertEqual(repo.get_issuance(self.old.issuance_id).status, ingest.IGNORED)
        self.assertEqual(self.status().remaining, 44)

        repo.set_intake_cutoff(None)
        ingest.reanalyse_issuance(self.old.issuance_id)
        self.assertEqual(repo.get_issuance(self.old.issuance_id).status, ingest.APPLIED)
        self.assertEqual(self.status().remaining, 42)

    def test_a_pending_earlier_issuance_is_closed_too(self) -> None:
        pending = ingest.ingest_issuance(
            NEEDS_REVIEW_EMAIL, "p-1", email_date=CUTOFF - timedelta(days=1), source="email"
        )
        self.assertEqual(pending.status, ingest.NEEDS_REVIEW)

        result = ingest.cancel_double_counted(CUTOFF)

        self.assertEqual(result.pending_closed, 1)
        self.assertEqual(repo.count_issuances(ingest.NEEDS_REVIEW), 0)

    def test_nothing_to_cancel_is_not_an_error(self) -> None:
        result = ingest.cancel_double_counted(CUTOFF - timedelta(days=30))
        self.assertEqual((result.cancelled, result.pending_closed), (0, 0))
        self.assertEqual(self.status().remaining, 42)

    def test_the_count_offered_matches_what_is_cancelled(self) -> None:
        self.add_recent_issuance()
        self.assertEqual(ingest.count_double_counted(CUTOFF), 1)
        self.assertEqual(ingest.cancel_double_counted(CUTOFF).cancelled, 1)

    def test_the_date_is_compared_as_a_moment_not_as_text(self) -> None:
        """
        An email sent at 02:00 Israel time is stored with a +03:00 offset, and
        precedes a midnight-UTC cutoff that its text sorts after.
        """
        israel_two_am = datetime(2026, 9, 8, 2, 0, tzinfo=timezone(timedelta(hours=3)))
        midnight_utc = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
        self.assertGreater(israel_two_am.isoformat(), midnight_utc.isoformat(),
                           "as text it sorts after the boundary")

        recent = SAMPLE_EMAIL.replace("שלום עם הנצח", "שלום דני כהן")
        ingest.ingest_issuance(recent, "tz-1", email_date=israel_two_am, source="email")

        # Compared as text this issuance would fall outside and nothing would be
        # cancelled; compared as a moment it is an hour before the boundary.
        self.assertEqual(ingest.count_double_counted(midnight_utc), 1)


class AnIssuanceTheCountAlreadyAbsorbedIsLeftAlone(DBTestCase):
    """
    The guard, expressed as selection rather than as a refusal. A movement
    stores a fixed delta computed against whatever was applied at the time, so
    dropping the issuance behind it would leave a credit with nothing to credit
    against — 46 of a standard of 44.
    """

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.item = repo.find_item_by_sku("1111")  # standard quantity 44
        ingest.ingest_issuance(
            SAMPLE_EMAIL, "old-1", email_date=CUTOFF - timedelta(days=2), source="email"
        )

    def status(self) -> inventory.ItemStatus:
        return inventory.status_for_item(repo.get_item(self.item.id))

    def test_a_reset_afterwards_takes_it_out_of_scope(self) -> None:
        """The reset was computed with this issuance included, so it stays."""
        ingest.record_reset(self.item)
        result = ingest.cancel_double_counted(CUTOFF)

        self.assertEqual(result.cancelled, 0)
        self.assertEqual(ingest.count_double_counted(CUTOFF), 0)

    def test_the_count_is_left_exactly_as_it_was(self) -> None:
        ingest.record_reset(self.item)
        self.assertEqual(self.status().remaining, 44)

        ingest.cancel_double_counted(CUTOFF)

        self.assertEqual(repo.count_issuances(ingest.APPLIED), 1)
        self.assertEqual(self.status().remaining, 44, "and certainly not 46")

    def test_a_stock_count_afterwards_does_the_same(self) -> None:
        """An edit computes its delta the same way, so it absorbs the same way."""
        ingest.record_edit(self.item, 30, "ספירה")
        self.assertEqual(ingest.cancel_double_counted(CUTOFF).cancelled, 0)

    def test_a_movement_from_before_the_issuance_makes_it_a_double_count(self) -> None:
        """
        This is the real case: the stock was counted, and the issuance arrived
        afterwards and was subtracted from the counted quantity. The timestamp is
        pushed back by hand because both are written to the second.
        """
        ingest.record_edit(self.item, 30, "ספירה")
        db.connect().execute("UPDATE adjustments SET created_at = ?", ("2020-01-01T00:00:00+00:00",))

        result = ingest.cancel_double_counted(CUTOFF)
        self.assertEqual(result.cancelled, 1)
        self.assertEqual(result.needs_recount, [], "no movement came after it")

    def test_an_item_counted_again_afterwards_is_named_not_left_wrong(self) -> None:
        """
        The one case this cannot fix alone. The issuance is a double count, so it
        goes — but this item was counted again after it arrived, and that count
        already absorbed it. Removing the issuance leaves the item too high, so
        it is reported by name for a fresh stock count.
        """
        ingest.record_edit(self.item, 30, "ספירה")
        db.connect().execute(
            "UPDATE adjustments SET created_at = ? WHERE id = (SELECT MAX(id) FROM adjustments)",
            ("2020-01-01T00:00:00+00:00",),
        )
        ingest.record_edit(self.item, 25, "ספירה חוזרת")
        db.connect().execute(
            "UPDATE adjustments SET created_at = ? WHERE id = (SELECT MAX(id) FROM adjustments)",
            ("2030-01-01T00:00:00+00:00",),
        )

        result = ingest.cancel_double_counted(CUTOFF)

        self.assertEqual(result.cancelled, 1)
        self.assertEqual(result.needs_recount, [(self.item.sku, self.item.name, 2)])


class FetchWindow(unittest.TestCase):
    """
    The mailbox scan is narrowed by the cutoff — a saving on network traffic
    only, since the binding decision is made on intake.
    """

    def setUp(self) -> None:
        self._creds = (config.IMAP_USER, config.IMAP_PASSWORD)
        config.IMAP_USER, config.IMAP_PASSWORD = "box@gmail.com", "app-password"

    def tearDown(self) -> None:
        config.IMAP_USER, config.IMAP_PASSWORD = self._creds

    def _scanned_from(self, **kwargs) -> str:
        fake = FakeIMAP([])
        with mock.patch.object(fetcher.imaplib, "IMAP4_SSL", fake):
            fetcher.fetch_recent(**kwargs)
        return fake.searches[0][1]

    def test_the_cutoff_narrows_the_scan(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        expected = (localtime.to_local(cutoff) - timedelta(days=1)).strftime("%d-%b-%Y")
        self.assertEqual(self._scanned_from(since=cutoff), expected)

    def test_a_cutoff_older_than_the_window_does_not_widen_it(self) -> None:
        ancient = datetime.now(timezone.utc) - timedelta(days=400)
        self.assertEqual(self._scanned_from(since=ancient), self._scanned_from())

    def test_the_date_is_taken_in_local_time(self) -> None:
        """
        At 01:00 in Israel it is still the previous day in UTC. The date chosen
        is the local one, so that is the day the scan starts from.
        """
        local_now = localtime.to_local(datetime.now(timezone.utc))
        cutoff = (local_now - timedelta(days=2)).replace(hour=1, minute=0, second=0, microsecond=0)
        expected = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")
        self.assertEqual(self._scanned_from(since=cutoff.astimezone(timezone.utc)), expected)

    def test_a_day_is_left_as_slack_for_the_mailbox_timezone(self) -> None:
        """
        SINCE compares whole dates, in the mailbox timezone, which need not
        agree with ours — so the scan starts a day early and intake does the
        exact filtering.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        scanned = datetime.strptime(self._scanned_from(since=cutoff), "%d-%b-%Y").date()
        self.assertLess(scanned, localtime.to_local(cutoff).date())


class TheScreen(DBTestCase):
    """The date is chosen in the browser, and what is typed there is local time."""

    def client(self):
        from tests.test_web import WSGIClient

        return WSGIClient()

    def test_saving_through_the_form(self) -> None:
        status, _, _ = self.client().post("/settings/intake-cutoff", {"cutoff": "2026-09-10T14:30"})
        self.assertEqual(status, 303)
        self.assertEqual(repo.get_intake_cutoff(), localtime.parse_input_value("2026-09-10T14:30"))

    def test_what_was_typed_is_read_as_local_time(self) -> None:
        self.client().post("/settings/intake-cutoff", {"cutoff": "2026-09-10T14:30"})
        self.assertEqual(localtime.format_dt(repo.get_intake_cutoff()), "10/09/2026 14:30")

    def test_clearing_through_the_form(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        self.client().post("/settings/intake-cutoff", {"clear": "1"})
        self.assertIsNone(repo.get_intake_cutoff())

    def test_an_unparseable_date_changes_nothing(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        self.client().post("/settings/intake-cutoff", {"cutoff": "לא תאריך"})
        self.assertEqual(repo.get_intake_cutoff(), CUTOFF)

    def test_the_dashboard_offers_the_last_reset_date(self) -> None:
        self.load_real_items()
        ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste")
        ingest.record_reset(repo.find_item_by_sku("1111"))

        _, _, body = self.client().get("/")
        self.assertIn(localtime.to_input_value(repo.last_reset_at()), body)
        self.assertIn(localtime.format_dt(repo.last_reset_at()), body)

    def test_the_cutoff_in_force_is_the_one_shown(self) -> None:
        repo.set_intake_cutoff(CUTOFF)
        _, _, body = self.client().get("/")
        self.assertIn(localtime.to_input_value(CUTOFF), body)

    def test_cancelling_the_past_from_the_screen(self) -> None:
        self.load_real_items()
        item = repo.find_item_by_sku("1111")  # standard quantity 44
        ingest.ingest_issuance(
            SAMPLE_EMAIL, "old-1", email_date=CUTOFF - timedelta(days=1), source="email"
        )
        repo.set_intake_cutoff(CUTOFF)
        self.assertEqual(inventory.status_for_item(item).remaining, 42)

        client = self.client()
        _, _, body = client.get("/")
        self.assertIn("/settings/cancel-past", body, "the offer is shown while there is a past")

        client.post("/settings/cancel-past")

        self.assertEqual(inventory.status_for_item(item).remaining, 44)
        _, _, body = client.get("/")
        self.assertNotIn("/settings/cancel-past", body, "and is gone once there is not")

    def test_the_offer_is_absent_until_a_date_is_saved(self) -> None:
        self.load_real_items()
        ingest.ingest_issuance(SAMPLE_EMAIL, "old-1", source="paste")
        _, _, body = self.client().get("/")
        self.assertNotIn("/settings/cancel-past", body)

    def test_cancelling_without_a_saved_date_is_refused(self) -> None:
        self.load_real_items()
        item = repo.find_item_by_sku("1111")
        ingest.ingest_issuance(SAMPLE_EMAIL, "old-1", source="paste")

        self.client().post("/settings/cancel-past")

        self.assertEqual(repo.count_issuances(ingest.APPLIED), 1)
        self.assertEqual(inventory.status_for_item(item).remaining, 42)


if __name__ == "__main__":
    unittest.main()
