"""
Loading of the .env file.

Without it, environment variables would have to be set again on every run —
especially tedious on Windows. A small implementation on top of the standard
library, with no external dependency.
"""
from __future__ import annotations

import os
from pathlib import Path


def parse(text: str) -> dict[str, str]:
    """
    KEY=VALUE format, one line at a time.

    Supported: blank lines, # comments, quotes around the value, and an export
    prefix. A value may itself contain = (a password hash, for instance), so
    only the first = is treated as the separator.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load(path: str | Path, override: bool = False) -> dict[str, str]:
    """
    Loads the file into the environment variables.

    By default a variable already present in the environment *wins* over the
    file, so that in the cloud the hosting service settings take precedence
    over a file left behind in the image by mistake.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}

    loaded = parse(text)
    for key, value in loaded.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded
