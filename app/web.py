"""
שכבת ווב מינימלית מעל WSGI (ספרייה סטנדרטית בלבד).

מספקת בדיוק את מה שהאתר צריך: ניתוב, פענוח טפסים כולל העלאת קובץ,
ועוגיית session חתומה. אין כאן framework כללי — רק מה שנדרש.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from http.cookies import SimpleCookie
from typing import Any, Callable
from urllib.parse import parse_qs, quote

from app import config

STATUS_TEXT = {
    200: "200 OK",
    303: "303 See Other",
    400: "400 Bad Request",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    413: "413 Payload Too Large",
}


@dataclass
class UploadedFile:
    filename: str
    content: bytes


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    form: dict[str, str]
    files: dict[str, UploadedFile]
    cookies: dict[str, str]
    headers: dict[str, str]
    remote_addr: str
    session: dict[str, Any] = field(default_factory=dict)
    #: נקבע ל-True כשמשנים את ה-session ויש לשלוח עוגייה חדשה.
    session_dirty: bool = False

    def get(self, name: str, default: str = "") -> str:
        return self.form.get(name, self.query.get(name, default))

    def get_int(self, name: str, default: int = 0) -> int:
        try:
            return int(self.get(name, str(default)).strip())
        except (TypeError, ValueError):
            return default

    def set_session(self, key: str, value: Any) -> None:
        self.session[key] = value
        self.session_dirty = True

    def clear_session(self) -> None:
        self.session.clear()
        self.session_dirty = True

    def pop_session(self, key: str, default: Any = None) -> Any:
        if key in self.session:
            self.session_dirty = True
            return self.session.pop(key)
        return default


@dataclass
class Response:
    body: bytes = b""
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def html(cls, markup: str, status: int = 200) -> "Response":
        return cls(body=markup.encode("utf-8"), status=status)

    @classmethod
    def redirect(cls, location: str) -> "Response":
        return cls(body=b"", status=303, headers=[("Location", location)])

    @classmethod
    def json(cls, payload: dict, status: int = 200) -> "Response":
        return cls(
            body=json.dumps(payload).encode("utf-8"),
            status=status,
            content_type="application/json; charset=utf-8",
        )


# ----------------------------------------------------------------- sessions

_SESSION_COOKIE = "ecs_session"


def _secret() -> bytes:
    return (config.SESSION_SECRET or "dev-insecure-secret").encode("utf-8")


def _sign(payload: bytes) -> str:
    mac = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(payload).decode()}.{base64.urlsafe_b64encode(mac).decode()}"


def _unsign(token: str) -> dict[str, Any]:
    try:
        raw, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(raw.encode())
        expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, base64.urlsafe_b64decode(sig.encode())):
            return {}
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except Exception:
        # עוגייה פגומה או מזויפת — מתייחסים אליה כאילו אין session.
        return {}


def _session_cookie_header(session: dict[str, Any]) -> tuple[str, str]:
    if not session:
        return ("Set-Cookie", f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
    token = _sign(json.dumps(session, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    parts = [
        f"{_SESSION_COOKIE}={token}",
        "Path=/",
        f"Max-Age={config.SESSION_MAX_AGE}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if config.COOKIE_SECURE:
        parts.append("Secure")
    return ("Set-Cookie", "; ".join(parts))


# -------------------------------------------------------------- form parsing


def _parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, UploadedFile]]:
    """
    פענוח multipart/form-data דרך מודול email — יציב יותר מפירוק ידני
    של ה-boundary, ומטפל נכון בקידודים.
    """
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=HTTP).parsebytes(header + body)
    form: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    if not message.is_multipart():
        return form, files

    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        payload = part.get_payload(decode=True) or b""
        if filename_match and filename_match.group(1):
            files[name] = UploadedFile(filename=filename_match.group(1), content=payload)
        else:
            form[name] = payload.decode("utf-8", errors="replace")
    return form, files


def build_request(environ: dict) -> Request:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    content_type = environ.get("CONTENT_TYPE", "")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    length = max(0, min(length, config.MAX_UPLOAD_BYTES))
    body = environ["wsgi.input"].read(length) if length else b""

    form: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    if body:
        if content_type.startswith("multipart/form-data"):
            form, files = _parse_multipart(body, content_type)
        elif content_type.startswith("application/x-www-form-urlencoded"):
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}

    cookie = SimpleCookie()
    cookie.load(environ.get("HTTP_COOKIE", ""))
    cookies = {k: v.value for k, v in cookie.items()}

    return Request(
        method=method,
        path=environ.get("PATH_INFO", "/") or "/",
        query={k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()},
        form=form,
        files=files,
        cookies=cookies,
        headers={
            "referer": environ.get("HTTP_REFERER", ""),
            "user-agent": environ.get("HTTP_USER_AGENT", ""),
        },
        remote_addr=environ.get("REMOTE_ADDR", "unknown"),
        session=_unsign(cookies.get(_SESSION_COOKIE, "")),
    )


# ------------------------------------------------------------------- router

Handler = Callable[[Request], Response]


class Router:
    """ניתוב פשוט עם פרמטרים מספריים בנתיב, למשל /items/{id}/reset."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def decorator(func: Handler) -> Handler:
            self._routes.append((method.upper(), regex, func))
            return func

        return decorator

    def get(self, pattern: str):
        return self.route("GET", pattern)

    def post(self, pattern: str):
        return self.route("POST", pattern)

    def dispatch(self, request: Request) -> Response:
        path_exists = False
        for method, regex, handler in self._routes:
            match = regex.match(request.path)
            if not match:
                continue
            path_exists = True
            if method != request.method:
                continue
            kwargs = {k: int(v) if v.isdigit() else v for k, v in match.groupdict().items()}
            return handler(request, **kwargs)
        if path_exists:
            return Response.html("<h1>405 — פעולה לא נתמכת בכתובת הזו</h1>", status=405)
        return Response.html("<h1>404 — הדף לא נמצא</h1>", status=404)


def make_wsgi_app(
    router: Router,
    on_request: Callable[[Request], None] | None = None,
    after_request: Callable[[], None] | None = None,
):
    """
    `after_request` רץ תמיד בסוף הטיפול, גם כשהבקשה נכשלה. משמש לשחרור
    משאבים שנצברו ל-thread — ראו main.py.
    """

    def application(environ, start_response):
        request = build_request(environ)
        if on_request:
            on_request(request)
        try:
            response = router.dispatch(request)
        except Exception:
            import logging
            import traceback

            logging.getLogger(__name__).error("שגיאה בטיפול בבקשה:\n%s", traceback.format_exc())
            response = Response.html("<h1>500 — שגיאה בשרת</h1>", status=500)
        finally:
            if after_request:
                after_request()

        headers = list(response.headers)
        headers.append(("Content-Type", response.content_type))
        headers.append(("Content-Length", str(len(response.body))))
        headers.append(("X-Content-Type-Options", "nosniff"))
        headers.append(("Referrer-Policy", "same-origin"))
        if request.session_dirty:
            headers.append(_session_cookie_header(request.session))
        start_response(STATUS_TEXT.get(response.status, f"{response.status} Status"), headers)
        return [response.body]

    return application


def safe_redirect_target(candidate: str, fallback: str = "/") -> str:
    """
    מונע הפניה לאתר חיצוני דרך פרמטר next או כותרת Referer.
    מתקבלים רק נתיבים פנימיים.
    """
    if not candidate:
        return fallback
    if candidate.startswith("//") or "://" in candidate:
        return fallback
    if not candidate.startswith("/"):
        return fallback
    return candidate


def query_string(**params: object) -> str:
    parts = [f"{k}={quote(str(v))}" for k, v in params.items() if v not in (None, "")]
    return ("?" + "&".join(parts)) if parts else ""
