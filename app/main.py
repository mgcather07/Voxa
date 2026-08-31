"""Voxa - FastAPI application for CUCM call telemetry and phone refresh planning."""

from __future__ import annotations

import csv
import io
import logging
from datetime import timezone
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import exports, history, locations, report_templates, reports
from .catalog import DEFAULT_FAMILY, FAMILIES, get_catalog
from .auth import (
    RedirectToLogin,
    get_backend,
    login_user,
    logout_user,
    redirect_to_login,
    require_user,
)
from .config import get_settings
from .db import get_session, init_db
from .models import (
    SWAP_STATUSES,
    CallStat,
    Location,
    LocationRule,
    Phone,
    SyncRun,
    User,
)
from .models import utcnow
from .sync import run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
)
log = logging.getLogger("voxa")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _timeago(value) -> str:
    """Human 'x ago' for the top-bar collection status line."""
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = (utcnow() - value).total_seconds()
    if seconds < 45:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{round(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24)}d ago"


templates.env.filters["timeago"] = _timeago

settings = get_settings()

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.session_max_age,
    https_only=settings.session_https_only,
    same_site="lax",
)


@app.exception_handler(RedirectToLogin)
def _handle_redirect_to_login(request: Request, exc: RedirectToLogin):
    return redirect_to_login(exc.next_url)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    if settings.secret_is_default and not settings.auth_disabled:
        log.warning(
            "SECRET_KEY is still the built-in default. Session cookies are "
            "forgeable. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if settings.auth_disabled:
        log.warning("AUTH_DISABLED=true - every request is treated as admin.")
    log.info("Database ready")


def _latest_run(session: Session) -> SyncRun | None:
    return session.scalars(
        select(SyncRun).order_by(desc(SyncRun.id)).limit(1)
    ).first()


def _fleet_total(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Phone)) or 0


def _ctx(request: Request, session: Session, user: User | None = None, **extra) -> dict:
    return {
        "request": request,
        "latest_run": _latest_run(session),
        "settings": settings,
        "swap_statuses": SWAP_STATUSES,
        "fleet_total": _fleet_total(session),
        "user": user,
        **extra,
    }


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "settings": settings,
            "next": next,
            "error": error,
        },
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: Session = Depends(get_session),
):
    backend = get_backend(settings)
    try:
        user = backend.authenticate(session, username, password)
    except NotImplementedError as exc:
        log.error("Auth backend error: %s", exc)
        return RedirectResponse(
            url="/login?error=Authentication+backend+is+misconfigured.",
            status_code=303,
        )

    if user is None:
        log.warning("Failed login for %r from %s", username, request.client.host)
        return RedirectResponse(
            url="/login?error=Incorrect+username+or+password.", status_code=303
        )

    login_user(request, user)
    user.last_login = utcnow()
    session.commit()
    log.info("Login: %s from %s", user.username, request.client.host)

    target = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(url=target, status_code=303)


@app.get("/logout")
@app.post("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request,
            session,
            user,
            summary=reports.summary(session),
            models=reports.by_model(session),
            sites=reports.by_site(session),
            unverified=reports.unverified_models(session),
            call_activity=reports.call_activity(session),
        ),
    )


# ---------------------------------------------------------------------------
# Phone list
# ---------------------------------------------------------------------------
ROW_CAP = 500


def _phone_conditions(
    q: str,
    site: str,
    model: str,
    life: list[str],
    status: str,
    swap: str,
) -> list:
    conditions = []
    if q:
        needle = f"%{q.strip()}%"
        conditions.append(
            or_(
                Phone.device_name.ilike(needle),
                Phone.description.ilike(needle),
                Phone.directory_number.ilike(needle),
                Phone.ip_address.ilike(needle),
                Phone.serial_number.ilike(needle),
                Phone.switch_name.ilike(needle),
            )
        )
    if site:
        conditions.append(Phone.site == site)
    if model:
        conditions.append(Phone.model_key == model)
    if life:
        conditions.append(Phone.lifecycle.in_(life))
    if status:
        conditions.append(Phone.registration_status == status)
    if swap:
        conditions.append(Phone.swap_status == swap)
    return conditions


def _phone_results(
    session: Session,
    q: str = "",
    site: str = "",
    model: str = "",
    life: list[str] | None = None,
    status: str = "",
    swap: str = "",
) -> dict:
    """Capped rows plus the true (uncapped) match count for the header line."""
    conditions = _phone_conditions(q, site, model, life or [], status, swap)

    match_count = session.scalar(
        select(func.count()).select_from(Phone).where(*conditions)
    ) or 0

    stmt = select(Phone).where(*conditions)
    stmt = stmt.order_by(Phone.site, Phone.device_name).limit(ROW_CAP)
    phones = list(session.scalars(stmt).all())

    return {
        "phones": phones,
        "match_count": match_count,
        "total": _fleet_total(session),
        "shown": len(phones),
    }


@app.get("/phones", response_class=HTMLResponse)
def phones_page(
    request: Request,
    q: str = "",
    site: str = "",
    model: str = "",
    life: list[str] = Query(default=[]),
    status: str = "",
    swap: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    result = _phone_results(session, q, site, model, life, status, swap)
    return templates.TemplateResponse(
        "phones.html",
        _ctx(
            request,
            session,
            user,
            filters={
                "q": q,
                "site": site,
                "model": model,
                "life": life,
                "status": status,
                "swap": swap,
            },
            **result,
        ),
    )


@app.get("/phones/rows", response_class=HTMLResponse)
def phones_rows(
    request: Request,
    q: str = "",
    site: str = "",
    model: str = "",
    life: list[str] = Query(default=[]),
    status: str = "",
    swap: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """HTMX partial - the table body plus the out-of-band header counts."""
    result = _phone_results(session, q, site, model, life, status, swap)
    return templates.TemplateResponse(
        "_phone_rows.html",
        {"request": request, "swap_statuses": SWAP_STATUSES, "oob": True, **result},
    )


@app.post("/phones/{phone_id}/swap", response_class=HTMLResponse)
def update_swap(
    request: Request,
    phone_id: int,
    swap_status: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    phone = session.get(Phone, phone_id)
    if phone and swap_status in SWAP_STATUSES:
        phone.swap_status = swap_status
        session.commit()
    return templates.TemplateResponse(
        "_swap_cell.html",
        {"request": request, "phone": phone, "swap_statuses": SWAP_STATUSES},
    )


@app.get("/phones/{phone_id}", response_class=HTMLResponse)
def phone_detail(
    request: Request,
    phone_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Phone 360: everything Voxa knows about one device, read-only."""
    phone = session.get(Phone, phone_id)
    if phone is None:
        raise HTTPException(status_code=404, detail="Phone not found")
    # Catalog facts (replacement role/requirements, PoE) for the planning card.
    info = get_catalog().lookup(phone.model_raw)
    timeline = history.device_timeline(session, phone.device_name)
    location = locations.resolve_phone(session, phone)
    call_stat = session.scalars(
        select(CallStat).where(CallStat.device_name == phone.device_name)
    ).first()
    return templates.TemplateResponse(
        "phone_detail.html",
        _ctx(
            request, session, user,
            phone=phone, info=info, timeline=timeline, location=location,
            call_stat=call_stat,
        ),
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    run: int | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Change history: what moved, appeared, dropped, re-registered per run."""
    runs = history.recent_runs(session, limit=30)
    diff = (
        history.diff_for_run(session, run) if run else history.latest_diff(session)
    )
    trend = history.fleet_trend(session, limit=12) if runs else []
    return templates.TemplateResponse(
        "history.html",
        _ctx(request, session, user, runs=runs, diff=diff, trend=trend, selected=run),
    )


# ---------------------------------------------------------------------------
# E911 locations
# ---------------------------------------------------------------------------
@app.get("/locations", response_class=HTMLResponse)
def locations_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "locations.html",
        _ctx(
            request, session, user,
            entries=locations.location_list(session),
            coverage=locations.coverage(session),
            match_types=locations.MATCH_TYPES,
        ),
    )


@app.post("/locations")
def location_add(
    name: str = Form(...),
    address: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    if name.strip():
        session.add(
            Location(
                name=name.strip(),
                address=address.strip() or None,
                notes=notes.strip() or None,
            )
        )
        session.commit()
    return RedirectResponse(url="/locations", status_code=303)


@app.post("/locations/{location_id}/rules")
def location_rule_add(
    location_id: int,
    match_type: str = Form(...),
    pattern: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    if (
        session.get(Location, location_id)
        and match_type in locations.MATCH_TYPES
        and pattern.strip()
    ):
        session.add(
            LocationRule(
                location_id=location_id,
                match_type=match_type,
                pattern=pattern.strip(),
            )
        )
        session.commit()
    return RedirectResponse(url="/locations", status_code=303)


@app.post("/locations/rules/{rule_id}/delete")
def location_rule_delete(
    rule_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    rule = session.get(LocationRule, rule_id)
    if rule:
        session.delete(rule)
        session.commit()
    return RedirectResponse(url="/locations", status_code=303)


@app.post("/locations/{location_id}/delete")
def location_delete(
    location_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    loc = session.get(Location, location_id)
    if loc:
        for rule in session.scalars(
            select(LocationRule).where(LocationRule.location_id == location_id)
        ).all():
            session.delete(rule)
        session.delete(loc)
        session.commit()
    return RedirectResponse(url="/locations", status_code=303)


@app.get("/locations/e911.csv")
def locations_export(
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    resolved = locations.resolve_all(session)
    phones = session.scalars(
        select(Phone).order_by(Phone.site, Phone.device_name)
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["device_name", "directory_number", "ip_address", "switch_name",
         "switch_port", "site", "location", "address"]
    )
    for phone in phones:
        loc = resolved.get(phone.id)
        writer.writerow([
            phone.device_name, phone.directory_number, phone.ip_address,
            phone.switch_name, phone.switch_port, phone.site,
            loc.name if loc else "", loc.address if loc else "",
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="voxa-e911.csv"'},
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@app.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "reports.html",
        _ctx(request, session, user, report_meta=report_templates.REPORT_META),
    )


@app.get("/reports/{key}.csv")
def report_csv(
    key: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    report = report_templates.build(session, key)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown report")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(report["columns"])
    writer.writerows(report["rows"])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="voxa-{key}.csv"'},
    )


@app.get("/reports/{key}.xlsx")
def report_xlsx(
    key: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    report = report_templates.build(session, key)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown report")
    data = exports.write_xlsx(report["columns"], report["rows"], report["title"])
    return StreamingResponse(
        iter([data]),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="voxa-{key}.xlsx"'},
    )


@app.get("/reports/{key}", response_class=HTMLResponse)
def report_view(
    request: Request,
    key: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    report = report_templates.build(session, key)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown report")
    return templates.TemplateResponse(
        "report_view.html", _ctx(request, session, user, report=report)
    )


# ---------------------------------------------------------------------------
# Refresh plan and PoE
# ---------------------------------------------------------------------------
def _clean_family(family: str) -> str:
    return family if family in FAMILIES else DEFAULT_FAMILY


@app.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    family: str = DEFAULT_FAMILY,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    family = _clean_family(family)
    plan = reports.refresh_plan(session, family)
    return templates.TemplateResponse(
        "plan.html",
        _ctx(
            request,
            session,
            user,
            family=family,
            plan=plan,
            mapping=reports.mapping_in_use(session, family),
        ),
    )


def _poe_context(session: Session, family: str) -> dict:
    switches = reports.poe_by_switch(session, family)
    total_current = round(sum(s["current_w"] for s in switches), 1)
    total_future = round(sum(s["future_w"] for s in switches), 1)
    delta = round(total_future - total_current, 1)
    span = max(total_current, total_future) or 1
    return {
        "family": family,
        "family_segments": FAMILIES,
        "switches": switches,
        "total_current": total_current,
        "total_future": total_future,
        "delta": delta,
        "pct_change": round(100 * delta / total_current) if total_current else 0,
        "total_ports": sum(s["ports"] for s in switches),
        "now_width": round(100 * total_current / span),
        "add_width": max(0, round(100 * delta / span)),
    }


@app.get("/poe", response_class=HTMLResponse)
def poe_page(
    request: Request,
    family: str = DEFAULT_FAMILY,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    family = _clean_family(family)
    context = _poe_context(session, family)
    # The replacement-family segmented control swaps just the body via HTMX.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "_poe_body.html", {"request": request, **context}
        )
    return templates.TemplateResponse(
        "poe.html", _ctx(request, session, user, **context)
    )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
@app.post("/sync")
def trigger_sync(
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    # Guard against a double-click, or two uvicorn workers both picking it up.
    running = session.scalars(
        select(SyncRun).where(SyncRun.status == "running").limit(1)
    ).first()
    if running:
        log.info("Sync already running (run %s); ignoring request", running.id)
        return RedirectResponse(url="/", status_code=303)

    log.info("Sync triggered by %s", user.username)
    background.add_task(run_sync)
    return RedirectResponse(url="/", status_code=303)


@app.get("/sync/status", response_class=HTMLResponse)
def sync_status(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "_sync_status.html", {"request": request, "latest_run": _latest_run(session)}
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
EXPORT_COLUMNS = [
    "device_name",
    "description",
    "directory_number",
    "site",
    "device_pool",
    "model_raw",
    "model_key",
    "lifecycle",
    "registration_status",
    "status_reason",
    "ip_address",
    "serial_number",
    "hardware_revision",
    "active_load",
    "switch_name",
    "switch_port",
    "vlan_id",
    "poe_class",
    "poe_watts",
    "replacement_key",
    "replacement_name",
    "replacement_poe_watts",
    "swap_status",
    "notes",
]


@app.get("/export.csv")
def export_csv(
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    phones = session.scalars(
        select(Phone).order_by(Phone.site, Phone.device_name)
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for phone in phones:
        writer.writerow([getattr(phone, column, "") for column in EXPORT_COLUMNS])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="voxa-phone-inventory.csv"'
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
