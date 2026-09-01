"""
הרצת השרת.

    python -m app.server

בענן: מגדירים PORT ומריצים את אותה פקודה.
"""
from __future__ import annotations

import logging
import sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer, make_server

from app import auth, config
from app.main import application, bootstrap


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """מספר בקשות במקביל — קליטת מיילים ברקע לא חוסמת את הממשק."""

    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logging.getLogger("access").info("%s %s", self.address_string(), format % args)


def _check_production_config() -> list[str]:
    """אזהרות שצריך לראות לפני שמעלים לענן."""
    warnings: list[str] = []
    if not auth.auth_configured():
        warnings.append(
            "APP_PASSWORD_HASH לא מוגדר — האתר פתוח לכל מי שמגיע לכתובת. "
            "ליצירת סיסמה: python -m app.hashpw"
        )
    if not config.SESSION_SECRET:
        warnings.append(
            "SESSION_SECRET לא מוגדר — עוגיות הכניסה ייחתמו במפתח ברירת מחדל ידוע. "
            "חובה להגדיר לפני העלאה לענן."
        )
    if config.imap_configured() and not config.COOKIE_SECURE:
        warnings.append("COOKIE_SECURE כבוי — להדליק כשהאתר רץ מאחורי HTTPS.")
    return warnings


def _force_utf8_output() -> None:
    """
    כל הודעות המערכת בעברית. אם הפלט מנותב לקובץ בווינדוס, פייתון משתמש
    בקידוד המקומי (cp1255) ושורת לוג בעברית מפילה את התהליך. כאן מכריחים UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # לא כל stream תומך — לא סיבה להיכשל


def main() -> int:
    _force_utf8_output()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log = logging.getLogger("server")

    bootstrap()
    for warning in _check_production_config():
        log.warning(warning)

    # ServerHandler מוסיף כותרת Server מלאה; מצמצמים חשיפת גרסאות.
    ServerHandler.server_software = "ecs"

    httpd = make_server(
        config.HOST,
        config.PORT,
        application,
        server_class=ThreadingWSGIServer,
        handler_class=QuietHandler,
    )
    log.info("מרכז ציוד שפלה — מאזין על http://%s:%s", config.HOST, config.PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("נעצר.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
