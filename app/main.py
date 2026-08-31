"""Voxa - FastAPI application for CUCM call telemetry and phone refresh planning."""

from __future__ import annotations

import csv
import io
import logging
import secrets
from datetime import timezone
from hashlib import sha256
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import (
    analytics,
    api,
    calls,
    capacity,
    certs,
    exports,
    history,
    importer,
    locations,
    report_templates,
    reports,
    settings_store,
    webhooks,
)
from .cucm_probe import probe as probe_cluster
from .catalog import DEFAULT_FAMILY, FAMILIES, get_catalog, reapply_to_phones
from .auth import (
    RedirectToLogin,
    get_backend,
    login_user,
    logout_user,
    redirect_to_login,
    require_admin,
    require_user,
)
from .config import get_settings
from .db import get_session, init_db
from .models import (
    SWAP_STATUSES,
    ApiToken,
    CallStat,
    CatalogOverride,
    Certificate,
    Cluster,
    ClusterNode,
    ClusterTestLog,
    Location,
    LocationRule,
    Phone,
    Setting,
    SyncRun,
    TrunkCapacity,
    User,
    Webhook,
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


def _comma(value):
    """Thousands separators for display: 1742 -> '1,742'. A no-op below 1000 and
    for anything non-numeric, so it is safe to apply to any integer count."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return value


templates.env.filters["comma"] = _comma


# Date/time display filters — 12-hour clock with AM/PM, no zero-padded day/hour.
# Written without glibc's %-I/%-d so they render the same on Linux and macOS.
def _fdt(v):          # "Aug 31, 2026 · 11:46 PM"
    if v is None:
        return ""
    return f"{v.strftime('%b')} {v.day}, {v.year} · {v.strftime('%I:%M %p').lstrip('0')}"


def _fdt_secs(v):     # "Aug 31, 2026 · 11:46:05 PM"
    if v is None:
        return ""
    return f"{v.strftime('%b')} {v.day}, {v.year} · {v.strftime('%I:%M:%S %p').lstrip('0')}"


def _fdt_compact(v):  # "Aug 31, 11:46 PM"
    if v is None:
        return ""
    return f"{v.strftime('%b')} {v.day}, {v.strftime('%I:%M %p').lstrip('0')}"


def _ftime(v):        # "11:46:05 PM"
    if v is None:
        return ""
    return v.strftime('%I:%M:%S %p').lstrip('0')


def _fdate(v):        # "Aug 31, 2026"
    if v is None:
        return ""
    return f"{v.strftime('%b')} {v.day}, {v.year}"


templates.env.filters["fdt"] = _fdt
templates.env.filters["fdt_secs"] = _fdt_secs
templates.env.filters["fdt_compact"] = _fdt_compact
templates.env.filters["ftime"] = _ftime
templates.env.filters["fdate"] = _fdate


# Hour-of-day (0-23 int) as a 12-hour clock, for busy-hour labels/axes.
def _hclock(h):       # 14 -> "2:00 PM"
    if h is None:
        return "—"
    return f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'}"


def _hlabel(h):       # 14 -> "2 PM"
    if h is None:
        return ""
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def _hax(h):          # 14 -> "2p", 0 -> "12a"
    if h is None:
        return ""
    return f"{h % 12 or 12}{'a' if h < 12 else 'p'}"


templates.env.filters["hclock"] = _hclock
templates.env.filters["hlabel"] = _hlabel
templates.env.filters["hax"] = _hax


def _days_until(v):   # whole days from now to a datetime; negative if past
    if v is None:
        return None
    return (v - utcnow()).days


templates.env.filters["days_until"] = _days_until


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
templates.env.globals["cause_label"] = calls.cause_label

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
app.include_router(api.router)


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
    cfg = settings_store.load(session)
    return {
        "request": request,
        "latest_run": _latest_run(session),
        "settings": settings,
        "app_name": cfg.app_name,
        "cluster_host": settings_store.primary_host(session),
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
            clusters=reports.clusters(session),
            nodes=session.scalars(
                select(ClusterNode).order_by(ClusterNode.cluster, ClusterNode.node_id)
            ).all(),
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
    cluster: str = "",
) -> list:
    conditions = []
    if cluster:
        conditions.append(Phone.cluster == cluster)
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
    cluster: str = "",
) -> dict:
    """Capped rows plus the true (uncapped) match count for the header line."""
    conditions = _phone_conditions(q, site, model, life or [], status, swap, cluster)

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


def _all_clusters(session: Session) -> list[str]:
    return sorted(
        c for c in session.scalars(select(Phone.cluster).distinct()).all() if c
    )


@app.get("/phones", response_class=HTMLResponse)
def phones_page(
    request: Request,
    q: str = "",
    site: str = "",
    model: str = "",
    life: list[str] = Query(default=[]),
    status: str = "",
    swap: str = "",
    cluster: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    result = _phone_results(session, q, site, model, life, status, swap, cluster)
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
                "cluster": cluster,
            },
            all_clusters=_all_clusters(session),
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
    cluster: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """HTMX partial - the table body plus the out-of-band header counts."""
    result = _phone_results(session, q, site, model, life, status, swap, cluster)
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
# CSV import
# ---------------------------------------------------------------------------
@app.get("/import", response_class=HTMLResponse)
def import_page(
    request: Request,
    result: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "import.html", _ctx(request, session, user, result=result)
    )


@app.post("/import")
async def import_submit(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    summary = importer.import_csv_text(session, text)
    session.commit()
    log.info(
        "CSV import by %s: %s created, %s updated, %s skipped",
        user.username, summary["created"], summary["updated"], summary["skipped"],
    )
    msg = (
        f"{summary['created']} added, {summary['updated']} updated"
        f"{', ' + str(summary['skipped']) + ' skipped' if summary['skipped'] else ''}"
    )
    return RedirectResponse(url=f"/import?result={msg}", status_code=303)


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
# Integrations (API tokens + webhooks) — admin only
# ---------------------------------------------------------------------------
def _integrations_ctx(request, session, user, **extra):
    return _ctx(
        request, session, user,
        tokens=session.scalars(select(ApiToken).order_by(ApiToken.id.desc())).all(),
        hooks=session.scalars(select(Webhook).order_by(Webhook.id.desc())).all(),
        webhook_events=webhooks.EVENTS,
        webhooks_master=settings_store.load(session).webhooks_enabled,
        **extra,
    )


@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        "integrations.html", _integrations_ctx(request, session, user)
    )


@app.post("/integrations/tokens", response_class=HTMLResponse)
def token_create(
    request: Request,
    label: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    plaintext = "voxa_" + secrets.token_urlsafe(32)
    session.add(ApiToken(
        label=label.strip() or "token",
        token_hash=sha256(plaintext.encode()).hexdigest(),
        created_by=user.username,
    ))
    session.commit()
    # Show the plaintext once — it is never recoverable after this.
    return templates.TemplateResponse(
        "integrations.html", _integrations_ctx(request, session, user, new_token=plaintext)
    )


@app.post("/integrations/tokens/{token_id}/delete")
def token_delete(
    token_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    row = session.get(ApiToken, token_id)
    if row:
        session.delete(row)
        session.commit()
    return RedirectResponse(url="/integrations", status_code=303)


@app.post("/integrations/webhooks")
def webhook_create(
    url: str = Form(...),
    events: list[str] = Form(default=[]),
    secret: str = Form(""),
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    if url.strip().startswith(("http://", "https://")):
        session.add(Webhook(
            url=url.strip(),
            events=",".join(events),
            secret=secret.strip() or None,
            enabled=False,
        ))
        session.commit()
    return RedirectResponse(url="/integrations", status_code=303)


@app.post("/integrations/webhooks/{hook_id}/toggle")
def webhook_toggle(
    hook_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    hook = session.get(Webhook, hook_id)
    if hook:
        hook.enabled = not hook.enabled
        session.commit()
    return RedirectResponse(url="/integrations", status_code=303)


@app.post("/integrations/webhooks/{hook_id}/delete")
def webhook_delete(
    hook_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    hook = session.get(Webhook, hook_id)
    if hook:
        session.delete(hook)
        session.commit()
    return RedirectResponse(url="/integrations", status_code=303)


@app.post("/integrations/webhooks/{hook_id}/test", response_class=HTMLResponse)
def webhook_test(
    request: Request,
    hook_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    hook = session.get(Webhook, hook_id)
    result = webhooks.send_test(session, hook) if hook else "not found"
    return templates.TemplateResponse(
        "integrations.html", _integrations_ctx(request, session, user, test_result=result)
    )


# ---------------------------------------------------------------------------
# Settings (operational config) — admin only
# ---------------------------------------------------------------------------
def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"on", "true", "1", "yes"}


def _run_cluster_probe(session: Session, cluster: Cluster) -> dict:
    """Test a cluster connection and record the result as a log entry, so the
    UI can show green/red status, the discovered nodes, and the failure reason."""
    conn = settings_store.ClusterConn(
        name=cluster.name, host=cluster.axl_host, user=cluster.cucm_user,
        password=cluster.cucm_password, axl_version=cluster.axl_version,
        verify_tls=cluster.verify_tls, phone_web_enabled=cluster.phone_web_enabled,
    )
    result = probe_cluster(conn)
    detail = "\n".join(
        f"[{'ok' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}"
        for c in result["checks"]
    )
    summary = "; ".join(
        f"{c['check']}: {'ok' if c['ok'] else 'FAIL'}" for c in result["checks"]
    )
    session.add(ClusterTestLog(
        cluster_id=cluster.id, ok=result["ok"], summary=summary,
        detail=detail, nodes="\n".join(result["nodes"]),
    ))
    cluster.last_test_at = utcnow()
    cluster.last_test_result = summary
    session.commit()
    return result


def _latest_logs(session: Session) -> dict[int, ClusterTestLog]:
    logs = session.scalars(
        select(ClusterTestLog).order_by(ClusterTestLog.id.desc())
    ).all()
    latest: dict[int, ClusterTestLog] = {}
    for log_row in logs:
        latest.setdefault(log_row.cluster_id, log_row)
    return latest


def _recent_logs(session: Session, cluster_id: int, limit: int = 6):
    return session.scalars(
        select(ClusterTestLog)
        .where(ClusterTestLog.cluster_id == cluster_id)
        .order_by(ClusterTestLog.id.desc())
        .limit(limit)
    ).all()


def _catalog_ctx(request, session, user, **extra):
    catalog = get_catalog()
    rows = []
    for m in reports.by_model(session):
        key = m["model_key"]
        if not key or key == "unknown":
            continue
        eff = catalog.effective(key)
        rows.append(
            {
                "key": key,
                "model_raw": m["model_raw"] or f"Cisco {key}",
                "count": m["count"],
                "poe_class": eff.get("poe_class"),
                "lifecycle": eff.get("lifecycle") or "unknown",
                "replacement": eff.get("replacement") or "none",
                "verified": bool(eff.get("verified")),
                "overridden": catalog.has_override(key),
            }
        )
    return _ctx(
        request, session, user,
        rows=rows,
        lifecycle_options=catalog.lifecycle_options(),
        replacement_options=catalog.replacement_options(),
        poe_classes=[(c, catalog.watts_for_class(c)) for c in catalog.poe_classes()],
        **extra,
    )


@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(
    request: Request,
    saved: int = 0,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        "catalog.html", _catalog_ctx(request, session, user, saved=bool(saved))
    )


@app.post("/catalog")
async def catalog_save(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    catalog = get_catalog()
    form = await request.form()
    for key in form.getlist("model_key"):
        base = catalog.base_entry(key)
        base_poe = int(base.get("poe_class") or 3)
        poe_v = int(form.get(f"poe_class__{key}") or base_poe)
        life_v = form.get(f"lifecycle__{key}") or "unknown"
        rep_raw = form.get(f"replacement__{key}") or "none"  # "none" or a key
        rep_norm = None if rep_raw == "none" else rep_raw
        ver_v = form.get(f"verified__{key}") is not None

        # A row that matches the shipped default is stored as no override at all,
        # so "set the fields back to default and Save" cleanly reverts.
        same = (
            poe_v == base_poe
            and life_v == (base.get("lifecycle") or "unknown")
            and rep_norm == base.get("replacement")
            and ver_v == bool(base.get("verified", False))
        )
        row = session.query(CatalogOverride).filter_by(model_key=key).one_or_none()
        if same:
            if row is not None:
                session.delete(row)
        else:
            if row is None:
                row = CatalogOverride(model_key=key)
                session.add(row)
            row.poe_class = poe_v
            row.lifecycle = life_v
            row.replacement = rep_raw
            row.verified = ver_v

    session.commit()            # persist overrides so the reload below sees them
    get_catalog.cache_clear()   # next get_catalog() rebuilds with the new edits
    n = reapply_to_phones(session)
    session.commit()            # persist the re-derived phone fields
    log.info("Catalog edited by %s; re-derived %s phones", user.username, n)
    return RedirectResponse(url="/catalog?saved=1", status_code=303)


@app.get("/certificates", response_class=HTMLResponse)
def certificates_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    cert_rows = session.scalars(
        select(Certificate).order_by(Certificate.host, Certificate.port)
    ).all()
    now = utcnow()
    valid = [c for c in cert_rows if not c.error and c.valid_to]
    expired = sum(1 for c in valid if c.valid_to < now)
    expiring = sum(1 for c in valid if 0 <= (c.valid_to - now).days < 90)
    last = max((c.checked_at for c in cert_rows), default=None)
    return templates.TemplateResponse(
        "certificates.html",
        _ctx(request, session, user, certs=cert_rows, last_checked=last,
             expired=expired, expiring=expiring),
    )


@app.post("/certificates/check")
def certificates_check(
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    summary = certs.collect(session)
    session.commit()
    log.info("Certificate check by %s: %s", user.username, summary)
    return RedirectResponse(url="/certificates?checked=1", status_code=303)


def _settings_ctx(request, session, user, **extra):
    groups: dict[str, list] = {}
    for key, kind, secret, group, label in settings_store.SCHEMA:
        groups.setdefault(group, []).append(
            {"key": key, "kind": kind, "secret": secret, "label": label}
        )
    clusters = session.scalars(select(Cluster).order_by(Cluster.id)).all()
    latest = _latest_logs(session)
    return _ctx(
        request, session, user,
        setting_groups=groups,
        values=settings_store.form_values(session),
        clusters=clusters,
        cluster_status=latest,
        cluster_logs={c.id: _recent_logs(session, c.id) for c in clusters},
        secret_mask=settings_store.SECRET_MASK,
        **extra,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    return templates.TemplateResponse("settings.html", _settings_ctx(request, session, user))


@app.post("/settings")
async def settings_save(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    form = await request.form()
    settings_store.save(session, dict(form))
    log.info("Settings updated by %s", user.username)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/clusters")
def cluster_add(
    name: str = Form(...),
    axl_host: str = Form(...),
    cucm_user: str = Form(""),
    cucm_password: str = Form(""),
    axl_version: str = Form("12.5"),
    verify_tls: str = Form(""),
    phone_web_enabled: str = Form(""),
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    if name.strip() and axl_host.strip():
        cluster = Cluster(
            name=name.strip(), axl_host=axl_host.strip(),
            cucm_user=cucm_user.strip(), cucm_password=cucm_password,
            axl_version=axl_version.strip() or "12.5",
            verify_tls=_truthy(verify_tls),
            phone_web_enabled=_truthy(phone_web_enabled),
        )
        session.add(cluster)
        session.commit()
        _run_cluster_probe(session, cluster)  # immediate green/red status
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/clusters/{cluster_id}")
def cluster_edit(
    cluster_id: int,
    name: str = Form(...),
    axl_host: str = Form(...),
    cucm_user: str = Form(""),
    cucm_password: str = Form(""),
    axl_version: str = Form("12.5"),
    verify_tls: str = Form(""),
    phone_web_enabled: str = Form(""),
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    c = session.get(Cluster, cluster_id)
    if c:
        c.name = name.strip() or c.name
        c.axl_host = axl_host.strip() or c.axl_host
        c.cucm_user = cucm_user.strip()
        # Masked/blank password means "keep the stored one".
        if cucm_password and cucm_password != settings_store.SECRET_MASK:
            c.cucm_password = cucm_password
        c.axl_version = axl_version.strip() or "12.5"
        c.verify_tls = _truthy(verify_tls)
        c.phone_web_enabled = _truthy(phone_web_enabled)
        session.commit()
        _run_cluster_probe(session, c)  # re-test after a change
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/clusters/{cluster_id}/toggle")
def cluster_toggle(
    cluster_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    c = session.get(Cluster, cluster_id)
    if c:
        c.enabled = not c.enabled
        session.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/clusters/{cluster_id}/delete")
def cluster_delete(
    cluster_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    c = session.get(Cluster, cluster_id)
    if c:
        session.delete(c)
        session.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/clusters/{cluster_id}/test", response_class=HTMLResponse)
def cluster_test(
    request: Request,
    cluster_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    c = session.get(Cluster, cluster_id)
    result = _run_cluster_probe(session, c) if c else {"checks": []}
    return templates.TemplateResponse(
        "settings.html",
        _settings_ctx(
            request, session, user,
            test_results=result["checks"], tested_id=cluster_id,
        ),
    )


# ---------------------------------------------------------------------------
# Calls (CDR)
# ---------------------------------------------------------------------------
@app.get("/calls", response_class=HTMLResponse)
def calls_page(
    request: Request,
    q: str = "",
    device: str = "",
    date_from: str = "",
    date_to: str = "",
    min_duration: int = 0,
    answered: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    results, match = calls.search_calls(
        session, q, device, date_from, date_to, min_duration, answered
    )
    return templates.TemplateResponse(
        "calls.html",
        _ctx(
            request, session, user,
            results=results, match=match,
            filters={
                "q": q, "device": device, "date_from": date_from,
                "date_to": date_to, "min_duration": min_duration,
                "answered": answered,
            },
        ),
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "analytics.html", _ctx(request, session, user, a=analytics.overview(session))
    )


@app.get("/capacity", response_class=HTMLResponse)
def capacity_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "capacity.html", _ctx(request, session, user, cap=capacity.overview(session))
    )


@app.post("/capacity/channels")
def capacity_set_channels(
    gateway: str = Form(...),
    channels: int = Form(0),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    row = session.scalars(
        select(TrunkCapacity).where(TrunkCapacity.gateway_name == gateway)
    ).first()
    if row is None:
        row = TrunkCapacity(gateway_name=gateway, channels=max(0, channels))
        session.add(row)
    else:
        row.channels = max(0, channels)
    session.commit()
    return RedirectResponse(url="/capacity", status_code=303)


@app.get("/calls/{call_key}", response_class=HTMLResponse)
def call_detail(
    request: Request,
    call_key: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    legs = calls.get_call(session, call_key)
    if not legs:
        raise HTTPException(status_code=404, detail="Call not found")
    quality = calls.quality_for_legs(session, legs)
    # Enrich the ladder with each phone's registration IP and CM node from
    # inventory (read-only DB lookup; the CDR carries a call-time IP but not the
    # CM node the phone registered to).
    dev_names = {
        d for leg in legs for d in (leg.orig_device, leg.dest_device)
        if d and d.startswith("SEP")
    }
    device_info: dict[str, dict] = {}
    if dev_names:
        for name, ip, node in session.execute(
            select(Phone.device_name, Phone.ip_address, Phone.cm_node)
            .where(Phone.device_name.in_(dev_names))
        ).all():
            device_info[name] = {"ip": ip, "cm_node": node}
    # Resolve a ladder CUCM node (an IP/hostname) to its friendly description.
    node_desc = {
        n.name: n.description
        for n in session.scalars(select(ClusterNode)).all()
        if n.description
    }
    ladder = calls.build_ladder(legs, device_info=device_info, node_desc=node_desc)
    return templates.TemplateResponse(
        "call_detail.html",
        _ctx(
            request, session, user,
            call_key=call_key, legs=legs, quality=quality, ladder=ladder,
        ),
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
        "has_snmp": any(s.get("available") is not None for s in switches),
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
