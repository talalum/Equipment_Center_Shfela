"""
Terminal text output.

The system handles Hebrew data — item names, email subjects, recipients —
and prints it from the CLI tools. On Windows, when output is redirected to a
file or a pipe, Python picks the local code page (cp1252/cp1255) and a single
Hebrew line kills the whole process with UnicodeEncodeError. This forces
UTF-8, so every tool that may print Hebrew data must call force_utf8_output()
at the start of its run.
"""
from __future__ import annotations

import sys


def force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # not every stream supports it — not a reason to fail
