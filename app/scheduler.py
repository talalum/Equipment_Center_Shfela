"""Background scheduling of the mail fetch — a single thread from the standard library."""
from __future__ import annotations

import logging
import threading

from app import config
from app.mail_sync import sync_once

log = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop() -> None:
    interval = max(60, config.POLL_MINUTES * 60)
    # A short first wait, so that server startup does not hang on a slow network.
    if _stop.wait(10):
        return
    while not _stop.is_set():
        try:
            sync_once()
        except Exception:  # no mailbox failure may bring the server down
            log.exception("Scheduled run failed")
        _stop.wait(interval)


def start() -> bool:
    """
    Starts the scheduled background fetch.

    Off when there are no mailbox credentials, and also when POLL_MINUTES is 0 —
    the mode in which fetching happens only on clicking "fetch emails" in the UI.
    """
    global _thread
    if _thread is not None:
        return True
    if not config.imap_configured():
        log.info("IMAP is not configured — automatic fetch is off. The paste screen works as usual.")
        return False
    if config.POLL_MINUTES <= 0:
        log.info('Automatic fetch is off by configuration — fetching only on clicking "fetch emails".')
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="mail-poller", daemon=True)
    _thread.start()
    log.info("Scheduled mail fetch is active, every %s minutes.", config.POLL_MINUTES)
    return True


def shutdown() -> None:
    global _thread
    _stop.set()
    _thread = None
