"""The site screens — Equipment Center Shfela."""
from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import auth, config, db, importer, ingest, inventory, localtime, mail_sync, repo, scheduler
from app.db import init_db
from app.parsing.normalize import normalize_sku
from app.web import Request, Response, Router, make_wsgi_app, safe_redirect_target

log = logging.getLogger(__name__)


TEMPLATES_DIR = config.BASE_DIR / "app" / "templates"
STATIC_DIR = config.BASE_DIR / "app" / "static"

router = Router()

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def asset(name: str) -> str:
    """A static URL carrying the file's own timestamp.

    Static files are served with an hour of caching, so without this a changed
    stylesheet reaches a browser that is still holding the previous one — the
    new markup styled by the old rules. The stamp changes with the file, which
    makes the browser fetch it, and stays put otherwise, so the caching is
    still worth having.
    """
    try:
        stamp = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={stamp}"


env.filters["local_dt"] = localtime.format_dt
env.globals["asset"] = asset
env.globals["signed"] = lambda n: f"+{n}" if n > 0 else str(n)
# The upload restrictions are read live, so the interface always shows what the
# server actually enforces — see config.py.
env.globals["upload_accept"] = config.upload_accept
env.globals["upload_types"] = config.upload_types_label
env.globals["upload_size"] = config.upload_size_label


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
    # With no password configured (local development) nothing is blocked, but a
    # warning is shown on every screen.
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

    # The date field is offered pre-filled: with the cutoff in force if there is
    # one, otherwise with the last reset to standard, which is the moment from
    # which the past stopped counting.
    cutoff = repo.get_intake_cutoff()
    last_reset = repo.last_reset_at()
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
        intake_cutoff=cutoff,
        last_reset_at=last_reset,
        cutoff_input=localtime.to_input_value(cutoff or last_reset),
        # A reset newer than the cutoff means the past has moved on and the date
        # has not — worth pointing out rather than leaving to be noticed.
        cutoff_behind_reset=bool(last_reset and (cutoff is None or cutoff < last_reset)),
        # How many issuances are being counted twice — the offer to clear them
        # is only shown when there is something to clear.
        cancellable_count=ingest.count_double_counted(cutoff) if cutoff else 0,
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


# -------------------------------------------------------- intake cutoff


@router.post("/settings/intake-cutoff")
def intake_cutoff_save(request: Request) -> Response:
    """
    The date from which emails are taken into stock. See
    `ingest.before_intake_cutoff` for why it exists.
    """
    if (redirect := login_required(request)) is not None:
        return redirect

    if request.get("clear"):
        repo.set_intake_cutoff(None)
        flash(request, f"ההגבלה בוטלה — נקלטים מיילים מכל {config.LOOKBACK_DAYS} הימים האחרונים.", "success")
        return back(request)

    moment = localtime.parse_input_value(request.get("cutoff"))
    if moment is None:
        flash(request, "תאריך לא תקין — לא נשמר שינוי.", "error")
        return back(request)

    repo.set_intake_cutoff(moment)
    message = f"מעתה ייקלטו למלאי רק מיילים מ-{localtime.format_dt(moment)} והלאה."
    # A date in the future blocks every email, and does it quietly — so it is
    # allowed (a warehouse may well want a start line a week from now) but never
    # without saying so.
    if moment > datetime.now(timezone.utc):
        flash(request, message + " התאריך עתידי, ולכן בינתיים לא ייקלט שום מייל.", "warn")
    else:
        flash(request, message, "success")
    return back(request)


@router.post("/settings/cancel-past")
def cancel_past_issuances(request: Request) -> Response:
    """
    Takes the issuances from before the saved date out of the count. Deliberately
    acts on the *saved* date and not on whatever is currently typed in the field,
    so that what is confirmed is what happens.
    """
    if (redirect := login_required(request)) is not None:
        return redirect

    cutoff = repo.get_intake_cutoff()
    if cutoff is None:
        flash(request, "קודם יש לבחור תאריך ולשמור אותו.", "error")
        return back(request)

    result = ingest.cancel_double_counted(cutoff)
    if not result.cancelled and not result.pending_closed:
        flash(request, f"אין הנפקות שנגרעו פעמיים לפני {localtime.format_dt(cutoff)} — לא בוצע שינוי.")
        return back(request)

    parts = [f"בוטלה הספירה של {result.cancelled} הנפקות שנגרעו פעמיים"]
    if result.items_affected:
        parts.append(f"{result.items_affected} פריטים תוקנו")
    if result.pending_closed:
        parts.append(f"{result.pending_closed} הנפקות שהמתינו לאישור נסגרו")
    message = " · ".join(parts) + "."

    # The items this could not fix on its own are named, never left to be
    # discovered — see ingest.cancel_double_counted.
    if result.needs_recount:
        listed = ", ".join(f"{sku} {name} (+{units})" for sku, name, units in result.needs_recount)
        flash(
            request,
            f'{message} שימי לב: {listed} — נספרו ידנית אחרי קליטת ההנפקה, ולכן הם כעת גבוהים '
            f'מדי בכמות שבסוגריים. יש לתקן אותם ב"עדכון מלאי".',
            "warn",
        )
    else:
        flash(request, message, "success")
    return back(request)


# -------------------------------------------------------------------- items


@router.get("/items")
def items_page(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    return render(
        request,
        "items.html",
        items=repo.list_items(),
        last_import=repo.last_import_run(),
        # Computed on every render rather than stored with the scan: the stock
        # moves on its own as issuance emails arrive, and a comparison shown
        # against yesterday's figures would be worse than none.
        comparison=pending_comparison(),
    )


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


def pending_comparison() -> importer.Comparison | None:
    """The file waiting for approval, against the state of the system right now."""
    pending = repo.get_pending_import()
    if pending is None:
        return None
    return importer.compare(importer.from_payload(pending.filename, pending.payload))


@router.post("/items/import")
def items_import(request: Request) -> Response:
    """
    Reads the uploaded file and stops there. Nothing is written until the user
    has seen the comparison and approved it — see /items/import/confirm.
    """
    if (redirect := login_required(request)) is not None:
        return redirect
    upload = request.files.get("file")
    if upload is None or not upload.content:
        flash(request, "לא נבחר קובץ.", "error")
        return Response.redirect("/items")
    if not config.upload_suffix_allowed(upload.filename):
        flash(request, f"אפשר להעלות קובץ {config.upload_types_label()} בלבד.", "error")
        return Response.redirect("/items")

    scanned = importer.scan(upload.content, upload.filename)
    if not scanned.usable:
        problems = " ".join(scanned.problems) or "לא נמצאו שורות נתונים בקובץ."
        flash(request, f"הקובץ לא נקלט — {problems}", "error")
        return Response.redirect("/items")

    repo.save_pending_import(scanned.filename, importer.to_payload(scanned))
    return Response.redirect("/items")


@router.post("/items/import/confirm")
def items_import_confirm(request: Request) -> Response:
    """Approval of the comparison — the only place the file is actually written."""
    if (redirect := login_required(request)) is not None:
        return redirect

    pending = repo.get_pending_import()
    if pending is None:
        flash(request, "אין ייבוא שממתין לאישור. ייתכן שכבר אושר או בוטל.", "error")
        return Response.redirect("/items")

    scanned = importer.from_payload(pending.filename, pending.payload)
    result = importer.apply(scanned, with_stock=True)
    repo.clear_pending_import()
    level = "error" if result.rejected and not result.total_ok else "success"
    flash(request, f"ייבוא הושלם — {result.summary()}.", level)
    return Response.redirect("/items")


@router.post("/items/import/discard")
def items_import_discard(request: Request) -> Response:
    if (redirect := login_required(request)) is not None:
        return redirect
    repo.clear_pending_import()
    flash(request, "הייבוא בוטל — לא בוצע שום שינוי.")
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
    # Filename only — the path cannot escape the directory.
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
    """Preparing the system for start-up: schema, initial load, and scheduling."""
    init_db()
    if not repo.list_items():
        csv_path = config.DEFAULT_IMPORT_CSV
        if csv_path.exists():
            result = importer.import_items(csv_path.read_bytes(), csv_path.name)
            log.info("Initial import from the standard-quantity file: %s", result.summary())
    scheduler.start()


# The database connection is closed at the end of every request. On SQLite that
# is cheap; on Postgres it is essential: the server opens a thread per request
# and each thread has its own connection — without an orderly release, the
# provider's connection quota runs out.
application = make_wsgi_app(router, after_request=db.close_thread_connection)
