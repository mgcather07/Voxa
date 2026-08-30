"""FastAPI application - CUCM phone inventory and refresh planner."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import reports
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
from .models import SWAP_STATUSES, Phone, SyncRun, User
from .models import utcnow
from .sync import run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
)
log = logging.getLogger("cucm-inventory")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

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


def _ctx(request: Request, session: Session, user: User | None = None, **extra) -> dict:
    return {
        "request": request,
        "latest_run": _latest_run(session),
        "settings": settings,
        "swap_statuses": SWAP_STATUSES,
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
        ),
    )


# ---------------------------------------------------------------------------
# Phone list
# ---------------------------------------------------------------------------
def _filtered_phones(
    session: Session,
    q: str = "",
    site: str = "",
    model: str = "",
    lifecycle: str = "",
    status: str = "",
    swap: str = "",
    limit: int = 500,
) -> list[Phone]:
    stmt = select(Phone)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
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
        stmt = stmt.where(Phone.site == site)
    if model:
        stmt = stmt.where(Phone.model_key == model)
    if lifecycle:
        stmt = stmt.where(Phone.lifecycle == lifecycle)
    if status:
        stmt = stmt.where(Phone.registration_status == status)
    if swap:
        stmt = stmt.where(Phone.swap_status == swap)
    stmt = stmt.order_by(Phone.site, Phone.device_name).limit(limit)
    return list(session.scalars(stmt).all())


def _filter_options(session: Session) -> dict:
    return {
        "all_sites": sorted(
            {s for s in session.scalars(select(Phone.site).distinct()).all() if s}
        ),
        "all_models": sorted(
            {
                m
                for m in session.scalars(select(Phone.model_key).distinct()).all()
                if m
            }
        ),
        "all_statuses": sorted(
            {
                s
                for s in session.scalars(
                    select(Phone.registration_status).distinct()
                ).all()
                if s
            }
        ),
    }


@app.get("/phones", response_class=HTMLResponse)
def phones_page(
    request: Request,
    q: str = "",
    site: str = "",
    model: str = "",
    lifecycle: str = "",
    status: str = "",
    swap: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    phones = _filtered_phones(session, q, site, model, lifecycle, status, swap)
    return templates.TemplateResponse(
        "phones.html",
        _ctx(
            request,
            session,
            user,
            phones=phones,
            filters={
                "q": q,
                "site": site,
                "model": model,
                "lifecycle": lifecycle,
                "status": status,
                "swap": swap,
            },
            **_filter_options(session),
        ),
    )


@app.get("/phones/rows", response_class=HTMLResponse)
def phones_rows(
    request: Request,
    q: str = "",
    site: str = "",
    model: str = "",
    lifecycle: str = "",
    status: str = "",
    swap: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """HTMX partial - just the table body."""
    phones = _filtered_phones(session, q, site, model, lifecycle, status, swap)
    return templates.TemplateResponse(
        "_phone_rows.html",
        {"request": request, "phones": phones, "swap_statuses": SWAP_STATUSES},
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


# ---------------------------------------------------------------------------
# Refresh plan and PoE
# ---------------------------------------------------------------------------
@app.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    plan = reports.replacement_plan(session)
    totals: dict[str, dict] = {}
    for row in plan:
        entry = totals.setdefault(
            row["replacement_key"],
            {"name": row["replacement_name"], "count": 0},
        )
        entry["count"] += row["count"]
    return templates.TemplateResponse(
        "plan.html",
        _ctx(
            request,
            session,
            user,
            plan=plan,
            totals=sorted(
                totals.items(), key=lambda kv: -kv[1]["count"]
            ),
            models=reports.by_model(session),
        ),
    )


@app.get("/poe", response_class=HTMLResponse)
def poe_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    switches = reports.poe_by_switch(session)
    return templates.TemplateResponse(
        "poe.html",
        _ctx(
            request,
            session,
            user,
            switches=switches,
            total_current=round(sum(s["current_w"] for s in switches), 1),
            total_future=round(sum(s["future_w"] for s in switches), 1),
        ),
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
            "Content-Disposition": 'attachment; filename="cucm-phone-inventory.csv"'
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
