"""תזמון משיכת המיילים ברקע — thread יחיד מהספרייה הסטנדרטית."""
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
    # המתנה ראשונה קצרה, כדי שהעלייה של השרת לא תיתקע על רשת איטית.
    if _stop.wait(10):
        return
    while not _stop.is_set():
        try:
            sync_once()
        except Exception:  # שום תקלה בתיבה לא מפילה את השרת
            log.exception("ריצת תזמון נכשלה")
        _stop.wait(interval)


def start() -> bool:
    """
    מפעיל משיכה מתוזמנת ברקע.

    כבוי כשאין פרטי תיבה, וגם כש-POLL_MINUTES הוא 0 — מצב שבו המשיכה
    מתבצעת רק בלחיצה על "משוך מיילים" בממשק.
    """
    global _thread
    if _thread is not None:
        return True
    if not config.imap_configured():
        log.info("IMAP לא מוגדר — משיכה אוטומטית כבויה. מסך ההדבקה עובד כרגיל.")
        return False
    if config.POLL_MINUTES <= 0:
        log.info('משיכה אוטומטית כבויה לפי ההגדרות — נמשך רק בלחיצה על "משוך מיילים".')
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="mail-poller", daemon=True)
    _thread.start()
    log.info("תזמון משיכת מיילים פעיל, כל %s דקות.", config.POLL_MINUTES)
    return True


def shutdown() -> None:
    global _thread
    _stop.set()
    _thread = None
