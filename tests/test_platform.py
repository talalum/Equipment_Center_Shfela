"""
Cross-platform correctness — Windows above all.

These tests were written after the system crashed on Windows over the timezone:
the operating system there has no timezone database, so ZoneInfo fails unless
the tzdata package is installed.
"""
from __future__ import annotations

import importlib
import os
import unittest
import zoneinfo
from datetime import datetime, timezone

# A path that does not exist, but is *absolute* on every operating system. On
# Windows an absolute path needs a drive letter, and reset_tzpath rejects a
# relative one — so a raw "/nonexistent-tzdb" used to break this test on
# Windows alone.
MISSING_TZDB = os.path.abspath("/nonexistent-tzdb")


class TimezoneFallback(unittest.TestCase):
    def setUp(self) -> None:
        self._tzpath = zoneinfo.TZPATH

    def tearDown(self) -> None:
        zoneinfo.reset_tzpath(to=list(self._tzpath))
        importlib.reload(importlib.import_module("app.main"))

    def test_startup_survives_a_missing_timezone_database(self) -> None:
        """Simulates Windows: no path holds a timezone database."""
        zoneinfo.reset_tzpath(to=[MISSING_TZDB])
        zoneinfo.ZoneInfo.clear_cache()
        main = importlib.reload(importlib.import_module("app.main"))

        self.assertIsNotNone(main.LOCAL_TZ, "a fallback timezone must be chosen rather than crashing")
        rendered = main._local_dt(datetime(2026, 9, 1, 14, 32, tzinfo=timezone.utc))
        self.assertRegex(rendered, r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")

    def test_unknown_timezone_name_falls_back(self) -> None:
        from app.main import _local_timezone

        self.assertIsNotNone(_local_timezone("Mars/Olympus_Mons"))

    def test_valid_timezone_is_used_when_available(self) -> None:
        from app.main import _local_timezone

        tz = _local_timezone("Asia/Jerusalem")
        # September in Israel = daylight saving time, UTC+3.
        offset = datetime(2026, 9, 1, 12, tzinfo=timezone.utc).astimezone(tz).utcoffset()
        self.assertEqual(offset.total_seconds(), 3 * 3600)


class DateRendering(unittest.TestCase):
    def test_naive_datetime_is_treated_as_utc(self) -> None:
        from app.main import _local_dt

        self.assertTrue(_local_dt(datetime(2026, 9, 1, 14, 32)))

    def test_none_renders_empty(self) -> None:
        from app.main import _local_dt

        self.assertEqual(_local_dt(None), "")


class NoRemovedStdlibModules(unittest.TestCase):
    """
    Modules removed in Python 3.13/3.14. The user runs 3.14, so using one of them
    would bring the system down on her machine alone.
    """

    REMOVED = {
        "cgi", "cgitb", "crypt", "imghdr", "telnetlib", "pipes", "nntplib",
        "sndhdr", "chunk", "audioop", "aifc", "sunau", "uu", "xdrlib", "mailcap",
    }

    def test_project_does_not_import_them(self) -> None:
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "app"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for module in self.REMOVED:
                if re.search(rf"^\s*(import|from)\s+{module}\b", source, re.MULTILINE):
                    offenders.append(f"{path.name}: {module}")
        self.assertEqual(offenders, [], f"modules removed from Python: {offenders}")


if __name__ == "__main__":
    unittest.main()
