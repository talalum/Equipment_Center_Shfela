"""HTTP-level tests — they call WSGI directly, without starting a server."""
from __future__ import annotations

import io
import unittest
from urllib.parse import urlencode

from tests.base import SAMPLE_EMAIL, DBTestCase, build_xlsx

from app import auth, config, repo


class WSGIClient:
    """A minimal client that keeps cookies between requests."""

    def __init__(self) -> None:
        from app.main import application

        self.app = application
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, data: dict | None = None) -> tuple[int, dict, str]:
        body = urlencode(data or {}, encoding="utf-8").encode() if data is not None else b""
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": "application/x-www-form-urlencoded" if data is not None else "",
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "wsgi.input": io.BytesIO(body),
        }
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = headers

        chunks = self.app(environ, start_response)
        self._store_cookies(captured["headers"])
        headers = {name.lower(): value for name, value in captured["headers"]}
        return captured["status"], headers, b"".join(chunks).decode("utf-8")

    def _store_cookies(self, headers: list[tuple[str, str]]) -> None:
        for name, value in headers:
            if name.lower() != "set-cookie":
                continue
            key, _, val = value.split(";", 1)[0].partition("=")
            if val:
                self.cookies[key] = val
            else:
                self.cookies.pop(key, None)

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, data: dict | None = None):
        return self.request("POST", path, data or {})

    def upload(self, path: str, filename: str, content: bytes, declared_length: int | None = None):
        """
        A multipart/form-data POST with a single file field named "file".

        `declared_length` overrides Content-Length, so the size limit can be
        tested without actually building a file of that size.
        """
        boundary = "----ecs-test-boundary"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        body = head + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_TYPE": f"multipart/form-data; boundary={boundary}",
            "CONTENT_LENGTH": str(len(body) if declared_length is None else declared_length),
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "wsgi.input": io.BytesIO(body),
        }
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = headers

        chunks = self.app(environ, start_response)
        self._store_cookies(captured["headers"])
        headers = {name.lower(): value for name, value in captured["headers"]}
        return captured["status"], headers, b"".join(chunks).decode("utf-8")


class PagesRender(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.client = WSGIClient()

    def test_every_page_returns_200(self) -> None:
        for path in ("/", "/items", "/issuances", "/review", "/movements", "/paste", "/healthz"):
            status, _, _ = self.client.get(path)
            self.assertEqual(status, 200, f"{path} returned {status}")

    def test_dashboard_lists_all_items(self) -> None:
        _, _, body = self.client.get("/")
        self.assertEqual(body.count('<tr class='), 76)
        self.assertIn("אין חוסרים", body)

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(self.client.get("/nope")[0], 404)

    def test_static_traversal_is_blocked(self) -> None:
        self.assertEqual(self.client.get("/static/..%2fmain.py")[0], 404)


class PasteJourney(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.client = WSGIClient()

    def test_paste_then_dashboard_shows_shortages(self) -> None:
        status, headers, _ = self.client.post("/paste", {"raw_text": SAMPLE_EMAIL})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")

        _, _, body = self.client.get("/")
        self.assertIn("נקלטו 7 פריטים.", body)
        self.assertIn('<span class="num">7</span>', body)

    def test_second_paste_changes_nothing(self) -> None:
        self.client.post("/paste", {"raw_text": SAMPLE_EMAIL})
        _, headers, _ = self.client.post("/paste", {"raw_text": SAMPLE_EMAIL})
        self.assertEqual(headers["location"], "/issuances")
        _, _, body = self.client.get("/")
        self.assertIn('<span class="num">7</span>', body)

    def test_unknown_sku_goes_to_review(self) -> None:
        broken = SAMPLE_EMAIL.replace("פלסטרים, מקט: 1102 - כמות: 1", "מזרן ואקום, מקט: 9999 - כמות: 1")
        _, headers, _ = self.client.post("/paste", {"raw_text": broken})
        self.assertEqual(headers["location"], "/review")
        _, _, body = self.client.get("/review")
        self.assertIn("9999", body)
        self.assertIn("לא נכנסה למלאי", body)

    def test_empty_paste_is_rejected(self) -> None:
        _, headers, _ = self.client.post("/paste", {"raw_text": "  "})
        self.assertEqual(headers["location"], "/paste")

    def test_filter_only_shortages(self) -> None:
        self.client.post("/paste", {"raw_text": SAMPLE_EMAIL})
        _, _, body = self.client.get("/?only_short=1")
        self.assertEqual(body.count('<tr class='), 7)

    def test_search_by_sku_and_by_name(self) -> None:
        _, _, by_sku = self.client.get("/?q=1111")
        self.assertEqual(by_sku.count('<tr class='), 1)
        _, _, by_name = self.client.get("/?q=פלסטר")
        self.assertGreaterEqual(by_name.count('<tr class='), 1)


class ButtonsOverHttp(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.client = WSGIClient()
        self.client.post("/paste", {"raw_text": SAMPLE_EMAIL})
        from app import repo

        self.item_id = repo.find_item_by_sku("1111").id

    def remaining(self) -> int:
        from app import inventory, repo

        return inventory.status_for_item(repo.get_item(self.item_id)).remaining

    def test_edit_uses_counted_quantity(self) -> None:
        self.client.post(f"/items/{self.item_id}/edit", {"actual_qty": "40", "reason": "ספירה"})
        self.assertEqual(self.remaining(), 40)

    def test_edit_rejects_negative(self) -> None:
        self.client.post(f"/items/{self.item_id}/edit", {"actual_qty": "-5"})
        self.assertEqual(self.remaining(), 42)

    def test_reset_restores_standard(self) -> None:
        self.client.post(f"/items/{self.item_id}/reset")
        self.assertEqual(self.remaining(), 44)

    def test_reset_all_clears_every_shortage(self) -> None:
        self.client.post("/items/reset-all")
        _, _, body = self.client.get("/")
        self.assertIn("אין חוסרים", body)
        _, _, movements = self.client.get("/movements")
        self.assertEqual(movements.count('data-label="סוג"'), 7)


class Authentication(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self._hash, self._secret = config.APP_PASSWORD_HASH, config.SESSION_SECRET
        config.APP_PASSWORD_HASH = auth.hash_password("correct-horse")
        config.SESSION_SECRET = "unit-test-secret"
        auth.throttle.attempts.clear()
        self.client = WSGIClient()

    def tearDown(self) -> None:
        config.APP_PASSWORD_HASH, config.SESSION_SECRET = self._hash, self._secret
        super().tearDown()

    def test_protected_pages_redirect_to_login(self) -> None:
        for path in ("/", "/items", "/review", "/movements", "/paste"):
            status, headers, _ = self.client.get(path)
            self.assertEqual(status, 303, path)
            self.assertTrue(headers["location"].startswith("/login"), path)

    def test_wrong_password_does_not_authenticate(self) -> None:
        status, _, body = self.client.post("/login", {"password": "nope", "next": "/"})
        self.assertEqual(status, 200)
        self.assertIn("סיסמה שגויה", body)
        self.assertEqual(self.client.get("/")[0], 303)

    def test_correct_password_grants_access(self) -> None:
        status, headers, _ = self.client.post("/login", {"password": "correct-horse", "next": "/"})
        self.assertEqual((status, headers["location"]), (303, "/"))
        self.assertEqual(self.client.get("/")[0], 200)

    def test_logout_ends_the_session(self) -> None:
        self.client.post("/login", {"password": "correct-horse", "next": "/"})
        self.client.get("/logout")
        self.assertEqual(self.client.get("/")[0], 303)

    def test_forged_cookie_is_rejected(self) -> None:
        self.client.cookies["ecs_session"] = "eyJ1c2VyIjoiYWRtaW4ifQ==.ZmFrZQ=="
        self.assertEqual(self.client.get("/")[0], 303)

    def test_open_redirect_is_blocked(self) -> None:
        _, headers, _ = self.client.post(
            "/login", {"password": "correct-horse", "next": "https://evil.example/x"}
        )
        self.assertEqual(headers["location"], "/")

    def test_lockout_after_repeated_failures(self) -> None:
        for _ in range(config.LOGIN_MAX_ATTEMPTS):
            self.client.post("/login", {"password": "nope", "next": "/"})
        _, _, body = self.client.post("/login", {"password": "correct-horse", "next": "/"})
        self.assertIn("יותר מדי ניסיונות", body)


class UploadRestrictions(DBTestCase):
    """The two restrictions on the import file: which type, and up to what size."""

    CSV = 'מק"ט,שם פריט,תקן\n7001,מזרן ואקום,4\n'

    def setUp(self) -> None:
        super().setUp()
        self.client = WSGIClient()

    def shrink_the_limit(self, limit: int) -> None:
        self.addCleanup(setattr, config, "MAX_UPLOAD_BYTES", config.MAX_UPLOAD_BYTES)
        config.MAX_UPLOAD_BYTES = limit

    def test_the_restrictions_are_shown_on_the_page(self) -> None:
        _, _, body = self.client.get("/items")
        self.assertIn(config.upload_types_label(), body)
        self.assertIn(config.upload_size_label(), body)
        self.assertIn(f'accept="{config.upload_accept()}"', body)

    def test_a_permitted_type_is_accepted_and_waits_for_approval(self) -> None:
        status, headers, _ = self.client.upload("/items/import", "standard.csv", self.CSV.encode("utf-8"))
        self.assertEqual((status, headers["location"]), (303, "/items"))
        self.assertIsNotNone(repo.get_pending_import())
        # Read but not written: the item appears only after the approval.
        self.assertIsNone(repo.find_item_by_sku("7001"))

    def test_an_excel_file_is_permitted_too(self) -> None:
        data = build_xlsx(['מק"ט', "שם פריט", "תקן מאושר"], [[7001, "מזרן ואקום", 4]])
        self.client.upload("/items/import", "standard.xlsx", data)
        self.assertIsNotNone(repo.get_pending_import())

    def test_an_upper_case_suffix_is_permitted_too(self) -> None:
        self.client.upload("/items/import", "STANDARD.CSV", self.CSV.encode("utf-8"))
        self.assertIsNotNone(repo.get_pending_import())

    def test_a_forbidden_type_is_rejected(self) -> None:
        # The content itself is a valid file — only the suffix is not permitted,
        # and that alone stops it, because accept is merely a hint to the browser.
        self.client.upload("/items/import", "standard.exe", self.CSV.encode("utf-8"))
        self.assertIsNone(repo.get_pending_import())
        _, _, body = self.client.get("/items")
        self.assertIn("אפשר להעלות קובץ", body)

    def test_an_over_sized_file_is_rejected_and_not_truncated(self) -> None:
        self.shrink_the_limit(64)
        rows = self.CSV + "".join(f"70{n:02d},פריט,1\n" for n in range(10, 40))
        status, _, body = self.client.upload("/items/import", "standard.csv", rows.encode("utf-8"))
        self.assertEqual(status, 413)
        self.assertIn(config.upload_size_label(), body)
        # Nothing at all got through — not even the rows that would have fitted
        # inside the limit, and no comparison waiting to be approved.
        self.assertIsNone(repo.get_pending_import())
        self.assertIsNone(repo.find_item_by_sku("7001"))

    def test_a_lying_content_length_does_not_get_past_the_limit(self) -> None:
        self.shrink_the_limit(64)
        status, _, _ = self.client.upload(
            "/items/import", "standard.csv", self.CSV.encode("utf-8"), declared_length=10_000_000
        )
        self.assertEqual(status, 413)


class ImportApproval(DBTestCase):
    """Upload, comparison, approval — and the two ways out of it."""

    HEADER = ['מק"ט', "שם פריט", "תקן מאושר", "מלאי לאחר הגעת משלוח "]

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.client = WSGIClient()

    def upload(self, rows: list, filename: str = "מלאי.xlsx"):
        return self.client.upload("/items/import", filename, build_xlsx(self.HEADER, rows))

    def remaining(self, sku: str) -> int:
        from app import inventory

        return inventory.status_for_item(repo.find_item_by_sku(sku)).remaining

    def test_the_comparison_appears_on_the_page_after_an_upload(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        _, _, body = self.client.get("/items")
        self.assertIn("אישור ייבוא", body)
        self.assertIn("אישור וייבוא", body)
        self.assertIn("מלאי.xlsx", body)

    def test_the_comparison_shows_the_before_the_after_and_the_difference(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        _, _, body = self.client.get("/items")
        self.assertIn(">19</span> ← <b>25</b>", body)
        self.assertIn("+6", body)

    def test_an_unchanged_item_is_not_listed(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 19], [1102, "פלסטר", 320, 400]])
        _, _, body = self.client.get("/items")
        self.assertIn("פריט אחד ללא שינוי", body)
        # Only the changed row is in the table. (Checked against the table and
        # not the whole dialog: the unchanged item is still named further down,
        # in the list of what the file leaves alone.)
        table = body.split("import-dialog")[1].split("</table>")[0]
        self.assertIn("1102", table)
        self.assertNotIn("1101", table)

    def test_items_absent_from_the_file_are_named_in_a_warning(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        _, _, body = self.client.get("/items")
        dialog = body.split("import-dialog")[1]
        self.assertIn("אינם מופיעים בקובץ", dialog)
        self.assertIn("תחבושת אישית", dialog)

    def test_nothing_changes_until_the_approval(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25], [9001, "פריט חדש", 5, 5]])
        self.assertEqual(self.remaining("1101"), 19)
        self.assertIsNone(repo.find_item_by_sku("9001"))
        # The only run on record is still the seed import from setUp.
        self.assertEqual(repo.last_import_run().filename, "Inventory_Report.csv")

    def test_the_approval_writes_the_stock(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        status, headers, _ = self.client.post("/items/import/confirm")
        self.assertEqual((status, headers["location"]), (303, "/items"))
        self.assertEqual(self.remaining("1101"), 25)
        self.assertIsNone(repo.get_pending_import(), "the approved import stops waiting")

    def test_the_approved_change_shows_up_in_the_movements_log(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        self.client.post("/items/import/confirm")
        _, _, movements = self.client.get("/movements")
        self.assertIn("מלאי.xlsx", movements)

    def test_cancelling_changes_nothing_and_clears_the_comparison(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        self.client.post("/items/import/discard")
        self.assertEqual(self.remaining("1101"), 19)
        self.assertIsNone(repo.get_pending_import())
        _, _, body = self.client.get("/items")
        self.assertNotIn("אישור ייבוא", body)

    def test_a_second_upload_replaces_the_first(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        self.upload([[1101, "חסם עורקים", 19, 30]])
        self.client.post("/items/import/confirm")
        self.assertEqual(self.remaining("1101"), 30)

    def test_approving_twice_does_not_import_twice(self) -> None:
        self.upload([[1101, "חסם עורקים", 19, 25]])
        self.client.post("/items/import/confirm")
        self.client.post("/items/import/confirm")
        _, _, body = self.client.get("/items")
        self.assertIn("אין ייבוא שממתין לאישור", body)
        self.assertEqual(self.remaining("1101"), 25)

    def test_a_file_with_no_usable_row_never_reaches_the_comparison(self) -> None:
        self.client.upload("/items/import", "broken.csv", "שם פריט,תקן\nפריט,5\n".encode("utf-8"))
        self.assertIsNone(repo.get_pending_import())
        _, _, body = self.client.get("/items")
        self.assertIn("חסרות עמודות חובה", body)

    def test_a_file_without_a_stock_column_says_the_stock_will_not_move(self) -> None:
        self.client.upload("/items/import", "std.csv", 'מק"ט,שם פריט,תקן\n1101,חסם עורקים,25\n'.encode("utf-8"))
        _, _, body = self.client.get("/items")
        self.assertIn("המלאי בארון לא ישתנה", body)
        self.client.post("/items/import/confirm")
        self.assertEqual(repo.find_item_by_sku("1101").standard_qty, 25)

    def test_the_import_needs_a_login_like_every_other_write(self) -> None:
        from app import auth, config

        hash_, secret = config.APP_PASSWORD_HASH, config.SESSION_SECRET
        config.APP_PASSWORD_HASH = auth.hash_password("correct-horse")
        config.SESSION_SECRET = "unit-test-secret"
        try:
            fresh = WSGIClient()
            status, headers, _ = fresh.upload("/items/import", "x.csv", "a,b\n1,2\n".encode("utf-8"))
            self.assertEqual(status, 303)
            self.assertTrue(headers["location"].startswith("/login"))
            self.assertIsNone(repo.get_pending_import())
            for path in ("/items/import/confirm", "/items/import/discard"):
                self.assertTrue(fresh.post(path)[1]["location"].startswith("/login"), path)
        finally:
            config.APP_PASSWORD_HASH, config.SESSION_SECRET = hash_, secret


class PasswordHashing(unittest.TestCase):
    def test_roundtrip(self) -> None:
        encoded = auth.hash_password("s3cret-password")
        self.assertTrue(auth.verify_password("s3cret-password", encoded))
        self.assertFalse(auth.verify_password("wrong", encoded))

    def test_salt_is_random(self) -> None:
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_malformed_hash_is_rejected(self) -> None:
        self.assertFalse(auth.verify_password("x", "not-a-real-hash"))


if __name__ == "__main__":
    unittest.main()
