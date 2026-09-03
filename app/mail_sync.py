"""The bridge between fetching the emails and taking them into the database."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import ingest, repo
from app.mail import fetcher

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    fetched: int = 0
    applied: int = 0
    needs_review: int = 0
    ignored: int = 0
    duplicates: int = 0
    error: str | None = None
    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def summary(self) -> str:
        if self.error:
            return f"שגיאה במשיכת מיילים: {self.error}"
        if not self.fetched:
            return "לא נמצאו מיילים חדשים."
        parts = [f"נמשכו {self.fetched} מיילים"]
        if self.applied:
            parts.append(f"{self.applied} נקלטו")
        if self.needs_review:
            parts.append(f"{self.needs_review} ממתינים לאישור")
        if self.ignored:
            parts.append(f"{self.ignored} ממרכז אחר")
        if self.duplicates:
            parts.append(f"{self.duplicates} כבר היו במערכת")
        return " · ".join(parts)


#: Result of the most recent run — shown in the dashboard header.
last_sync: SyncResult | None = None

# One fetch at a time: the manual button and the scheduler must not run together.
_lock = threading.Lock()


def sync_once() -> SyncResult:
    """Fetches the recent emails and takes them in. Safe to run repeatedly."""
    global last_sync
    if not _lock.acquire(blocking=False):
        return last_sync or SyncResult(error="משיכה כבר רצה כרגע.")
    try:
        result = SyncResult()
        try:
            # Bodies are fetched only for emails not yet in the database — a repeat scan is cheap.
            emails = fetcher.fetch_recent(
                is_known=lambda mid: repo.find_issuance_by_message_id(mid) is not None
            )
        except Exception as exc:  # network/auth — must not bring the server down
            log.exception("Mail fetch failed")
            result.error = str(exc)
            last_sync = result
            return result

        result.fetched = len(emails)
        for item in emails:
            outcome = ingest.ingest_issuance(
                raw_text=item.body,
                message_id=item.message_id,
                email_date=item.date,
                source="email",
            )
            if outcome.duplicate:
                result.duplicates += 1
            elif outcome.status == ingest.APPLIED:
                result.applied += 1
            elif outcome.status == ingest.IGNORED:
                result.ignored += 1
            else:
                result.needs_review += 1

        last_sync = result
        return result
    finally:
        _lock.release()
