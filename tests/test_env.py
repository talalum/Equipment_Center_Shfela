"""קריאת וכתיבת קובץ .env."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app import env_file
from app.setup import _write_env


class ParseEnv(unittest.TestCase):
    def test_basic_pairs(self) -> None:
        self.assertEqual(env_file.parse("A=1\nB=two"), {"A": "1", "B": "two"})

    def test_comments_and_blank_lines_ignored(self) -> None:
        self.assertEqual(env_file.parse("# הערה\n\nA=1\n  # עוד\n"), {"A": "1"})

    def test_value_may_contain_equals(self) -> None:
        """hash של סיסמה מכיל $ ו-=; אסור שייחתך."""
        encoded = "pbkdf2_sha256$240000$abc==$def=="
        self.assertEqual(env_file.parse(f"APP_PASSWORD_HASH={encoded}")["APP_PASSWORD_HASH"], encoded)

    def test_quotes_are_stripped(self) -> None:
        self.assertEqual(env_file.parse('A="hello world"\nB=\'x\''), {"A": "hello world", "B": "x"})

    def test_export_prefix_allowed(self) -> None:
        self.assertEqual(env_file.parse("export A=1"), {"A": "1"})

    def test_line_without_equals_skipped(self) -> None:
        self.assertEqual(env_file.parse("nonsense\nA=1"), {"A": "1"})

    def test_whitespace_trimmed(self) -> None:
        self.assertEqual(env_file.parse("  A = 1  "), {"A": "1"})


class LoadEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".env"
        self._saved = {k: os.environ.get(k) for k in ("ECS_T1", "ECS_T2")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_values_reach_the_environment(self) -> None:
        self.path.write_text("ECS_T1=fromfile\n", encoding="utf-8")
        env_file.load(self.path)
        self.assertEqual(os.environ["ECS_T1"], "fromfile")

    def test_existing_environment_wins(self) -> None:
        """בענן ההגדרות של שירות האירוח חייבות לגבור על קובץ שנשאר בתמונה."""
        os.environ["ECS_T2"] = "from-cloud"
        self.path.write_text("ECS_T2=from-file\n", encoding="utf-8")
        env_file.load(self.path)
        self.assertEqual(os.environ["ECS_T2"], "from-cloud")

    def test_override_flag(self) -> None:
        os.environ["ECS_T2"] = "old"
        self.path.write_text("ECS_T2=new\n", encoding="utf-8")
        env_file.load(self.path, override=True)
        self.assertEqual(os.environ["ECS_T2"], "new")

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(env_file.load(Path(self._tmp.name) / "nope.env"), {})

    def test_bom_is_handled(self) -> None:
        """Notepad בווינדוס שומר עם BOM."""
        self.path.write_text("﻿ECS_T1=ok\n", encoding="utf-8")
        self.assertEqual(env_file.load(self.path)["ECS_T1"], "ok")


class WriteEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".env"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_file_when_missing(self) -> None:
        _write_env(self.path, {"A": "1"})
        self.assertEqual(env_file.parse(self.path.read_text(encoding="utf-8")), {"A": "1"})

    def test_updates_in_place_and_keeps_comments(self) -> None:
        self.path.write_text("# הגדרות\nA=old\nB=keep\n", encoding="utf-8")
        _write_env(self.path, {"A": "new"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# הגדרות", text)
        self.assertEqual(env_file.parse(text), {"A": "new", "B": "keep"})

    def test_appends_new_keys(self) -> None:
        self.path.write_text("A=1\n", encoding="utf-8")
        _write_env(self.path, {"B": "2"})
        self.assertEqual(env_file.parse(self.path.read_text(encoding="utf-8")), {"A": "1", "B": "2"})

    def test_no_duplicate_keys_after_repeated_writes(self) -> None:
        _write_env(self.path, {"A": "1"})
        _write_env(self.path, {"A": "2"})
        _write_env(self.path, {"A": "3"})
        lines = [l for l in self.path.read_text(encoding="utf-8").splitlines() if l.startswith("A=")]
        self.assertEqual(lines, ["A=3"])

    def test_password_hash_survives_a_roundtrip(self) -> None:
        from app.auth import hash_password, verify_password

        encoded = hash_password("some-real-password")
        _write_env(self.path, {"APP_PASSWORD_HASH": encoded})
        loaded = env_file.parse(self.path.read_text(encoding="utf-8"))["APP_PASSWORD_HASH"]
        self.assertTrue(verify_password("some-real-password", loaded))


if __name__ == "__main__":
    unittest.main()
