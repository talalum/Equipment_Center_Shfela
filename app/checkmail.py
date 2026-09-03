"""
Checking the mailbox connection.

    py -m app.checkmail

Connects, reports what it found, marks no email as read and does not touch
stock. Its purpose is to answer "are the settings correct?" without waiting for
the next fetch cycle.
"""
from __future__ import annotations

import imaplib
import socket
import ssl
import sys

from app import config
from app.console import force_utf8_output
from app.mail import fetcher
from app.parsing import issuance_parser


def _explain(error: Exception) -> str:
    """Turns network and authentication errors into an explanation to act on."""
    text = str(error)
    if isinstance(error, imaplib.IMAP4.error):
        lowered = text.lower()
        if "invalid credentials" in lowered or "authentication failed" in lowered:
            return (
                "Authentication was rejected. The usual causes:\n"
                "  - The account's regular password was entered instead of a 16-character App Password.\n"
                "  - The App Password was revoked, or was created for a different account.\n"
                "  - There is a typo in the email address.\n"
                "  To set it up again:  py -m app.setup"
            )
        return f"The server rejected the request: {text}"
    if isinstance(error, socket.gaierror):
        return f"The address {config.IMAP_HOST} was not found — probably no internet connection, or a wrong host name."
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "The connection hung. A firewall may be blocking outbound port 993."
    if isinstance(error, ssl.SSLError):
        return f"Encryption failure on the connection: {text}"
    if isinstance(error, OSError):
        return f"Network failure: {text}"
    return text


def main() -> int:
    # Email subjects and parse errors are printed below, and both are Hebrew.
    force_utf8_output()
    print("\n=== Mailbox connection check ===\n")

    if not config.imap_configured():
        print("No mailbox details are configured.")
        print("  To set them up:  py -m app.setup")
        print("\nUntil then, emails can be pasted by hand on the 'paste email' screen.\n")
        return 1

    print(f"  Server:   {config.IMAP_HOST}:{config.IMAP_PORT}")
    print(f"  Account:  {config.IMAP_USER}")
    print(f"  Folder:   {config.IMAP_FOLDER}")
    print(f"  Password: configured ({len(config.IMAP_PASSWORD)} characters)")
    if len(config.IMAP_PASSWORD) != 16:
        print("            Note: a Google App Password is 16 characters long.")
    print("\nConnecting...\n")

    try:
        # No is_known — the check shows everything in the time window, including
        # what has already been taken in.
        emails = fetcher.fetch_recent()
    except Exception as exc:  # the explanation matters more than a traceback
        print("✗ Connection failed.\n")
        print(_explain(exc))
        print()
        return 1

    print("✓ Connection succeeded.\n")

    if not emails:
        print(f"No emails in the last {config.LOOKBACK_DAYS} days in the mailbox.")
        print("  If you expected some — check that the issuance emails really do arrive")
        print("  at this account, and that they were sent within this period.\n")
        return 0

    print(f"Found {len(emails)} emails in the last {config.LOOKBACK_DAYS} days:\n")
    recognised = 0
    for item in emails[:20]:
        parsed = issuance_parser.parse(item.body)
        if parsed.ok and issuance_parser.center_matches(parsed):
            recognised += 1
            mark, detail = "✓", f"{len(parsed.lines)} items · issuer: {parsed.issuer or '—'}"
        elif parsed.ok:
            mark, detail = "•", f'another center: {parsed.center or "unknown"} — will not be counted'
        else:
            mark, detail = "✗", parsed.errors[0]
        print(f"  {mark} {item.subject or '(no subject)'}")
        print(f"      {detail}")

    print(f"\n{recognised} of them are recognised as issuances of {issuance_parser.load_format().expected_center}.")
    print("\nNothing entered stock and the mailbox state is unchanged — this is a check only.")
    print("For an actual intake: run the server and click the ⟳ fetch-emails button.\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
