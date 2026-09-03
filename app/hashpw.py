"""
Helper for generating the hash of the login password.

    python -m app.hashpw

Paste the output into APP_PASSWORD_HASH in the runtime environment.
"""
from __future__ import annotations

import getpass
import sys

from app.auth import hash_password


def main() -> int:
    password = getpass.getpass("New password: ")
    if len(password) < 8:
        print("Password is too short — at least 8 characters.", file=sys.stderr)
        return 1
    if password != getpass.getpass("Again, to confirm: "):
        print("Passwords don't match.", file=sys.stderr)
        return 1
    print("\nAdd this line to your environment variables:\n")
    print(f"APP_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
