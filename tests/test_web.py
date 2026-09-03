"""HTTP-level tests — they call WSGI directly, without starting a server."""
from __future__ import annotations

import io
import unittest
from urllib.parse import urlencode

from tests.base import SAMPLE_EMAIL, DBTestCase

from app import auth, config


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
        for name, value in captured["headers"]:
            if name.lower() == "set-cookie":
                pair = value.split(";", 1)[0]
                key, _, val = pair.partition("=")
                if val:
                    self.cookies[key] = val
                else:
                    self.cookies.pop(key, None)
        headers = {name.lower(): value for name, value in captured["headers"]}
        return captured["status"], headers, b"".join(chunks).decode("utf-8")

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, data: dict | None = None):
        return self.request("POST", path, data or {})


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
