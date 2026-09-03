"""
Initial site setup — login password, signing key, and mailbox connection.

    py -m app.setup

This tool writes the values to a .env file next to the code. That file is
not committed to git and is never sent anywhere — it stays on your machine.
"""
from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

from app import config, env_file
from app.auth import hash_password
from app.console import force_utf8_output

MIN_PASSWORD_LENGTH = 8


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "y/n"
    while True:
        answer = input(f"{prompt} ({hint}) [{'y' if default else 'n'}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("  Didn't understand that. Please type 'y' or 'n'.")


def _ask_password() -> str:
    while True:
        # getpass doesn't echo characters while typing.
        password = getpass.getpass("New site password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"  Too short — must be at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if password != getpass.getpass("Again, to confirm: "):
            print("  Passwords don't match. Let's try again.")
            continue
        return password


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """
    Updates existing keys in place and appends new ones at the end.
    Comments and untouched lines are kept as-is.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    remaining = dict(updates)
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    force_utf8_output()
    env_path = config.ENV_PATH
    existing = env_file.parse(env_path.read_text(encoding="utf-8-sig")) if env_path.is_file() else {}
    updates: dict[str, str] = {}

    print("\n=== Site setup — Shfela Equipment Center ===\n")
    print(f"Settings will be written to: {env_path}\n")

    # --- 1. login password ---
    print("--- 1. Site login password ---")
    if existing.get("APP_PASSWORD_HASH"):
        print("A password is already set.")
        if not _ask_yes_no("Replace it?", default=False):
            print("  Keeping the existing password.")
        else:
            updates["APP_PASSWORD_HASH"] = hash_password(_ask_password())
            print("  ✓ Password updated.")
    else:
        print("This is the password required to log in to the site.")
        updates["APP_PASSWORD_HASH"] = hash_password(_ask_password())
        print("  ✓ Password set.")

    # --- 2. signing key ---
    print("\n--- 2. Cookie signing key ---")
    if existing.get("SESSION_SECRET"):
        print("  ✓ Already set, leaving it as-is (replacing it would log you out).")
    else:
        updates["SESSION_SECRET"] = secrets.token_urlsafe(48)
        print("  ✓ Generated automatically as a random string. Nothing to remember.")

    # --- 3. mailbox ---
    print("\n--- 3. Connecting the dedicated Gmail account ---")
    print("You can skip this and set it up later; until then you can paste emails manually on the site.")
    if _ask_yes_no("Set it up now?", default=False):
        user = _ask("Gmail address", existing.get("IMAP_USER", ""))
        print("\nYou need a 16-character App Password — not the account's regular password.")
        print("How to get one: myaccount.google.com/apppasswords (requires 2-Step Verification to be enabled).")
        app_password = getpass.getpass("App Password: ").replace(" ", "")
        if user and app_password:
            if len(app_password) != 16:
                print(f"  Note: {len(app_password)} characters were pasted, not 16. If the connection fails, that's why.")
            updates["IMAP_USER"] = user
            updates["IMAP_PASSWORD"] = app_password
            print("  ✓ Mailbox details saved.")

            print("\nWhen should emails be fetched?")
            if _ask_yes_no("Fetch automatically in the background every few minutes?", default=False):
                updates["POLL_MINUTES"] = _ask("Every how many minutes", existing.get("POLL_MINUTES", "5"))
                print(f'  ✓ Automatic fetch every {updates["POLL_MINUTES"]} minutes.')
            else:
                updates["POLL_MINUTES"] = "0"
                print('  ✓ Only when clicking "Fetch emails" on the site.')
        else:
            print("  Skipped — details were not fully entered.")
    else:
        print("  Skipped.")

    # --- 4. cloud ---
    print("\n--- 4. Where the site will run ---")
    print("COOKIE_SECURE sends the login cookie only over an encrypted (HTTPS) connection.")
    print("When running locally (http://localhost) it must stay off, otherwise you won't be able to log in.")
    if _ask_yes_no("Will the site run in the cloud behind HTTPS?", default=False):
        updates["COOKIE_SECURE"] = "1"
        print("  ✓ Enabled.")
    else:
        updates["COOKIE_SECURE"] = "0"
        print("  ✓ Disabled — suitable for local development.")

    _write_env(env_path, updates)

    print("\n" + "=" * 50)
    print("Settings saved. Now run:\n")
    print("    py -m app.server        (Windows)")
    print("    python3 -m app.server   (Mac/Linux)\n")
    print("The .env file stays on your machine only and is not committed to git.")
    print("=" * 50 + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled. No changes were saved.")
        sys.exit(1)
