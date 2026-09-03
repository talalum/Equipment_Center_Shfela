"""
Running the server.

    python -m app.server

In the cloud: set PORT and run the same command.
"""
from __future__ import annotations

import logging
import sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer, make_server

from app import auth, config, db
from app.console import force_utf8_output
from app.main import application, bootstrap


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Several requests in parallel — background mail intake does not block the UI."""

    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logging.getLogger("access").info("%s %s", self.address_string(), format % args)


def _check_production_config() -> list[str]:
    """Warnings that need to be seen before deploying to the cloud."""
    warnings: list[str] = []
    if not auth.auth_configured():
        warnings.append(
            "APP_PASSWORD_HASH is not set — the site is open to anyone who reaches the address. "
            "To create a password: python -m app.hashpw"
        )
    if not config.SESSION_SECRET:
        warnings.append(
            "SESSION_SECRET is not set — login cookies will be signed with a known default key. "
            "It must be set before deploying to the cloud."
        )
    if config.imap_configured() and not config.COOKIE_SECURE:
        warnings.append(
            "COOKIE_SECURE is off — correct and required when running locally. "
            "Turn it on only when the site goes to the cloud behind HTTPS."
        )
    if config.COOKIE_SECURE and not config.DATABASE_URL:
        # COOKIE_SECURE on means this is a cloud deployment, where the container
        # filesystem is wiped on every deploy. Running on SQLite there loses the
        # manual stock movements, which nothing can reconstruct.
        warnings.append(
            "DATABASE_URL is not set, so the site is running on a SQLite file inside "
            "the container. In the cloud that file is wiped on every deploy and the "
            "warehouse will look empty. Set DATABASE_URL to the Postgres connection string."
        )
    return warnings


def main() -> int:
    force_utf8_output()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log = logging.getLogger("server")

    # Logged before bootstrap so that it is visible even if the connection then
    # fails: the first question when a deployment looks empty is which database
    # it is talking to.
    log.info("Database: %s", db.describe_backend())

    bootstrap()
    for warning in _check_production_config():
        log.warning(warning)

    # ServerHandler adds a full Server header; keep version disclosure to a minimum.
    ServerHandler.server_software = "ecs"

    httpd = make_server(
        config.HOST,
        config.PORT,
        application,
        server_class=ThreadingWSGIServer,
        handler_class=QuietHandler,
    )
    log.info("Equipment Center Shfela — listening on http://%s:%s", config.HOST, config.PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
