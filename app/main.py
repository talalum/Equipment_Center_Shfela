"""מסכי האתר — מרכז ציוד שפלה."""
from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import auth, config, db, importer, ingest, inventory, mail_sync, repo, scheduler
from app.db import init_db
from app.parsing.normalize import normalize_sku
from app.web import Request, Response, Router, make_wsgi_app, safe_redirect_target

log = logging.getLogger(__name__)


def _local_timezone(name: str):
    """
    אזור הזמן לתצוגה.

    בווינדוס אין מסד אזורי זמן במערכת ההפעלה, ולכן ZoneInfo נכשל שם אלא אם
    מותקנת חבילת tzdata. במקרה כזה נופלים לשעון של המחשב עצמו — עדיף להציג
    שעה נכונה מהמערכת מאשר להפיל את השרת בגלל תצוגת תאריך.
    """
    try:
        return ZoneInfo(name)
    except Exception:
        fallback = datetime.now().astimezone().tzinfo
        log.warning(
            'לא נמצא אזור הזמן "%s" (נפוץ בווינדוס בלי חבילת tzdata) — '
            "מוצג שעון המחשב במקום. להתקנה: pip install tzdata",
            name,
        )
        return fallback


LOCAL_TZ = _local_timezone(config.TZ_NAME)
TEMPLATES_DIR = config.BASE_DIR / "app" / "templates"
STATIC_DIR = config.BASE_DIR / "app" / "static"

router = Router()

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _local_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")


env.filters["local_dt"] = _local_dt
env.globals["signed"] = lambda n: f"+{n}" if n > 0 else str(n)


# ------------------------------------------------------------------ helpers


def render(request: Request, template: str, **context) -> Response:
    context.setdefault("path", request.path)
    context.setdefault("auth_configured", auth.auth_configured())
    context.setdefault("flash", request.pop_session("flash"))
    context.setdefault("last_sync", mail_sync.last_sync)
    context.setdefault("imap_configured", config.imap_configured())
    return Response.html(env.get_template(template).render(**context))


def flash(request: Request, message: str, level: str = "info") -> None:
    request.set_session("flash", {"message": message, "level": level})


def back(request: Request, fallback: str = "/") -> Response:
    return Response.redirect(safe_redirect_target(request.headers.get("referer", ""), fallback))


def authenticated(request: Request) -> bool:
    # אם לא הוגדרה סיסמה (פיתוח מקומי) אין חסימה, אבל אזהרה מוצגת בכל מסך.
    return not auth.auth_configured() or bool(request.session.get("user"))


def login_required(request: Request) -> Response | None:
    if authenticated(request):
        return None
    return Response.redirect("/login" + ("?next=" + request.path if request.path != "/" else ""))


# -------------------------------------------------------------------- login


@router.get("/login")
def login_form(request: Request) -> Response:
    if authenticated(request):
        return Response.redirect("/")
    return render(request, "login.html", next=safe_redirect_target(request.get("next"), "/"), error=None)


@router.post("/login")
def login_submit(request: Request) -> Response:
    target = safe_redirect_target(request.get("next"), "/")
    locked = auth.throttle.seconds_remaining(request.remote_addr)
    if locked:
        return render(
            request,
            "login.html",
            next=target,
            error=f"יותר מדי ניסיונות. נסי שוב בעוד {locked} שניות.",
        )
    if auth.check_password(request.get("password")):
        auth.throttle.reset(request.remote_addr)
        request.set_session("user", "admin")
        return Response.redirect(target)
    auth.throttle.record_failure(request.remote_addr)
    return render(request, "login.html", next=target, error="סיסמה שגויה.")


@router.get("/logout")
def logout(request: Request) -> Response:
    request.clear_session()
    return Response.redirect("/login")


# ---------------------------------------------------------------- dashboard


@router.get("/")
def dashboard(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect

    statuses = inventory.status_for_all(repo.list_items(include_inactive=False))
    total_items = len(statuses)
    shortage_count = sum(1 for s in statuses if s.in_shortage)

    term = request.get("q").strip()
    only_short = request.get("only_short")
    if term:
        sku_term = normalize_sku(term)
        statuses = [
            s for s in statuses if term in s.item.name or (sku_term and sku_term in s.item.sku)
        ]
    if only_short:
        statuses = [s for s in statuses if s.in_shortage]

    sort = request.get("sort", "shortage")
    direction = request.get("direction", "desc")
    statuses = inventory.sort_statuses(statuses, sort=sort, direction=direction)

    pending_count = repo.count_issuances(ingest.NEEDS_REVIEW)
    return render(
        request,
        "dashboard.html",
        statuses=statuses,
        total_items=total_items,
        shortage_count=shortage_count,
        q=term,
        only_short=only_short,
        sort=sort,
        direction=direction,
        pending_count=pending_count,
    )


@router.post("/items/{item_id}/edit")
def item_edit(request: Request, item_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    item = repo.get_item(item_id)
    if item is None:
        flash(request, "הפריט לא נמצא.", "error")
        return back(request)

    actual = request.get_int("actual_qty", -1)
    if actual < 0:
        flash(request, "יש להקליד כמות שאינה שלילית.", "error")
        return back(request)

    delta = ingest.record_edit(item, actual, request.get("reason", "עדכון ידני"))
    if delta is None:
        flash(request, f"{item.name}: הכמות כבר הייתה {actual} — לא נרשמה תנועה.")
    else:
        flash(request, f"{item.name}: נרשמה תנועה של {delta:+d}. הכמות עודכנה ל-{actual}.", "success")
    return back(request)


@router.post("/items/{item_id}/reset")
def item_reset(request: Request, item_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    item = repo.get_item(item_id)
    if item is None:
        flash(request, "הפריט לא נמצא.", "error")
        return back(request)
    delta = ingest.record_reset(item)
    if delta is None:
        flash(request, f"{item.name}: כבר בתקן — לא נרשמה תנועה.")
    else:
        flash(request, f"{item.name}: אופס לתקן ({delta:+d}). נשאר {item.standard_qty}.", "success")
    return back(request)


@router.post("/items/reset-all")
def reset_all(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    changed = ingest.reset_all_shortages()
    if changed:
        flash(request, f"{changed} פריטים אופסו לתקן.", "success")
    else:
        flash(request, "אין פריטים בחוסר — לא בוצע שינוי.")
    return Response.redirect("/")


# -------------------------------------------------------------------- items


@router.get("/items")
def items_page(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    return render(request, "items.html", items=repo.list_items(), last_import=repo.last_import_run())


@router.post("/items/{item_id}/update")
def item_update(request: Request, item_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    item = repo.get_item(item_id)
    if item is None:
        flash(request, "הפריט לא נמצא.", "error")
        return back(request, "/items")
    standard = request.get_int("standard_qty", -1)
    if standard < 0:
        flash(request, "התקן לא יכול להיות שלילי.", "error")
        return back(request, "/items")
    name = request.get("name").strip() or item.name
    repo.update_item(item_id, name, standard, bool(request.get("active")))
    flash(request, f"{name} עודכן.", "success")
    return back(request, "/items")


@router.post("/items/new")
def item_new(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    sku = normalize_sku(request.get("sku"))
    name = request.get("name").strip()
    if not sku or not name:
        flash(request, 'צריך מק"ט ושם פריט.', "error")
        return back(request, "/items")
    if repo.find_item_by_sku(sku):
        flash(request, f'מק"ט {sku} כבר קיים.', "error")
        return back(request, "/items")
    repo.create_item(sku, name, request.get_int("standard_qty"))
    flash(request, f"הפריט {name} נוסף.", "success")
    return back(request, "/items")


@router.post("/items/import")
def items_import(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    upload = request.files.get("file")
    if upload is None or not upload.content:
        flash(request, "לא נבחר קובץ.", "error")
        return Response.redirect("/items")
    result = importer.import_items(upload.content, upload.filename)
    level = "error" if result.rejected and not result.total_ok else "success"
    flash(request, f"ייבוא הושלם — {result.summary()}.", level)
    return Response.redirect("/items")


# ---------------------------------------------------------------- issuances


@router.get("/issuances")
def issuances_page(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    issuances = repo.list_issuances((ingest.APPLIED, ingest.IGNORED))
    return render(request, "issuances.html", issuances=issuances)


@router.get("/review")
def review_page(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    return render(
        request,
        "review.html",
        issuances=repo.list_issuances((ingest.NEEDS_REVIEW,), newest_first=False),
        items=repo.list_items(include_inactive=False),
    )


@router.post("/review/{issuance_id}/assign/{line_id}")
def review_assign(request: Request, issuance_id: int, line_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    line = repo.get_line(line_id)
    item = repo.get_item(request.get_int("item_id"))
    if line is None or item is None or line["issuance_id"] != issuance_id:
        flash(request, "השורה או הפריט לא נמצאו.", "error")
        return back(request, "/review")
    repo.assign_line_item(line_id, item.id)
    flash(request, f'מק"ט {line["raw_sku"]} שויך לפריט {item.name}.', "success")
    return back(request, "/review")


@router.post("/review/{issuance_id}/create-item/{line_id}")
def review_create_item(request: Request, issuance_id: int, line_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    line = repo.get_line(line_id)
    if line is None or line["issuance_id"] != issuance_id:
        flash(request, "השורה לא נמצאה.", "error")
        return back(request, "/review")
    item = repo.find_item_by_sku(line["raw_sku"])
    if item is None:
        sku = normalize_sku(line["raw_sku"])
        item_id = repo.create_item(sku, line["raw_name"] or sku, request.get_int("standard_qty"))
        item = repo.get_item(item_id)
    repo.assign_line_item(line_id, item.id)
    flash(request, f'נוצר פריט חדש: {item.name} (מק"ט {item.sku}).', "success")
    return back(request, "/review")


@router.post("/issuances/{issuance_id}/reanalyse")
def issuance_reanalyse(request: Request, issuance_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    changed, message = ingest.reanalyse_issuance(issuance_id)
    flash(request, message, "success" if changed else "info")
    return back(request, "/issuances")


@router.post("/reanalyse-all")
def reanalyse_all(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    counts = ingest.reanalyse_unapplied()
    if not counts["total"]:
        flash(request, "אין הנפקות שממתינות לניתוח מחדש.")
    else:
        flash(
            request,
            f"נותחו מחדש {counts['total']} הנפקות — "
            f"{counts['applied']} נקלטו למלאי, "
            f"{counts['needs_review']} ממתינות לאישור, "
            f"{counts['ignored']} לא רלוונטיות.",
            "success" if counts["applied"] else "info",
        )
    return back(request, "/issuances")


@router.post("/review/{issuance_id}/approve")
def review_approve(request: Request, issuance_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    ok, message = ingest.approve_issuance(issuance_id)
    flash(request, message, "success" if ok else "error")
    return back(request, "/review")


@router.post("/review/{issuance_id}/ignore")
def review_ignore(request: Request, issuance_id: int) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    if repo.get_issuance(issuance_id) is None:
        flash(request, "ההנפקה לא נמצאה.", "error")
        return back(request, "/review")
    ingest.ignore_issuance(issuance_id)
    flash(request, "ההנפקה סומנה כלא רלוונטית ולא תיספר במלאי.")
    return back(request, "/review")


# -------------------------------------------------------------------- paste


@router.get("/paste")
def paste_form(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    return render(request, "paste.html")


@router.post("/paste")
def paste_submit(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    raw_text = request.get("raw_text")
    if not raw_text.strip():
        flash(request, "לא הודבק טקסט.", "error")
        return Response.redirect("/paste")

    result = ingest.ingest_issuance(
        raw_text=raw_text,
        message_id=ingest.synthetic_message_id(raw_text),
        source="paste",
    )
    if result.duplicate:
        flash(request, result.message)
        return Response.redirect("/issuances")
    if result.status == ingest.APPLIED:
        flash(request, result.message, "success")
        return Response.redirect("/")
    if result.status == ingest.NEEDS_REVIEW:
        flash(request, result.message, "error")
        return Response.redirect("/review")
    flash(request, result.message)
    return Response.redirect("/issuances")


# ---------------------------------------------------------------- movements


@router.get("/movements")
def movements_page(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    return render(request, "movements.html", adjustments=repo.list_adjustments())


# --------------------------------------------------------------------- sync


@router.post("/sync")
def sync_now(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    if not config.imap_configured():
        flash(request, "חיבור לתיבה לא מוגדר. אפשר להדביק מייל ידנית.", "error")
        return back(request)
    result = mail_sync.sync_once()
    flash(request, result.summary(), "error" if result.error else "success")
    return back(request)


@router.get("/healthz")
def healthz(_: Request) -> Response:
    return Response.json({"status": "ok"})


# ------------------------------------------------------------------- static


@router.get("/static/{name}")
def static_file(_: Request, name: str) -> Response:
    # שם קובץ בלבד — הנתיב לא יכול לצאת מהתיקייה.
    candidate = (STATIC_DIR / Path(name).name).resolve()
    if not candidate.is_file() or STATIC_DIR.resolve() not in candidate.parents:
        return Response.html("<h1>404</h1>", status=404)
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return Response(
        body=candidate.read_bytes(),
        content_type=f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
        headers=[("Cache-Control", "public, max-age=3600")],
    )


# -------------------------------------------------------------------- setup


def bootstrap() -> None:
    """הכנת המערכת לעלייה: סכימה, טעינה ראשונית ותזמון."""
    init_db()
    if not repo.list_items():
        csv_path = config.DEFAULT_IMPORT_CSV
        if csv_path.exists():
            result = importer.import_items(csv_path.read_bytes(), csv_path.name)
            log.info("ייבוא ראשוני מקובץ התקן: %s", result.summary())
    scheduler.start()


# החיבור למסד נסגר בסוף כל בקשה. ב-SQLite זה זול, וב-Postgres זה הכרחי:
# השרת פותח thread לכל בקשה, ולכל thread חיבור משלו — בלי שחרור מסודר
# מכסת החיבורים של הספק נגמרת.
application = make_wsgi_app(router, after_request=db.close_thread_connection)
