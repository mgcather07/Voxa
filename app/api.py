"""Read-only JSON API (v1) for integrations — CMDB, dashboards, scripts.

Bearer-token auth (SHA-256 hashed in the DB). Every endpoint is a read; there is
no write API and nothing here touches CUCM. Manage tokens at /api-tokens.
"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analytics, calls, locations, report_templates, reports
from .catalog import get_catalog
from .db import get_session
from .models import ApiToken, CallStat, Phone, utcnow

router = APIRouter(prefix="/api/v1", tags=["api"])


def require_api_token(
    request: Request, session: Session = Depends(get_session)
) -> ApiToken:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth[7:].strip()
    row = session.scalars(
        select(ApiToken).where(
            ApiToken.token_hash == sha256(token.encode()).hexdigest()
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    row.last_used_at = utcnow()
    session.commit()
    return row


def _phone_dict(p: Phone) -> dict:
    return {
        "id": p.id,
        "device_name": p.device_name,
        "description": p.description,
        "directory_number": p.directory_number,
        "model": p.model_raw or p.model_key,
        "model_key": p.model_key,
        "lifecycle": p.lifecycle,
        "cluster": p.cluster,
        "site": p.site,
        "device_pool": p.device_pool,
        "registration_status": p.registration_status,
        "ip_address": p.ip_address,
        "active_load": p.active_load,
        "serial_number": p.serial_number,
        "switch_name": p.switch_name,
        "switch_port": p.switch_port,
        "vlan_id": p.vlan_id,
        "poe_class": p.poe_class,
        "poe_watts": p.poe_watts,
        "replacement": p.replacement_name,
        "swap_status": p.swap_status,
    }


@router.get("/phones")
def api_phones(
    q: str = "",
    cluster: str = "",
    life: str = "",
    limit: int = Query(500, le=5000),
    session: Session = Depends(get_session),
    _: ApiToken = Depends(require_api_token),
):
    stmt = select(Phone)
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(Phone.device_name.ilike(needle))
    if cluster:
        stmt = stmt.where(Phone.cluster == cluster)
    if life:
        stmt = stmt.where(Phone.lifecycle == life)
    rows = session.scalars(stmt.order_by(Phone.device_name).limit(limit)).all()
    return {"count": len(rows), "phones": [_phone_dict(p) for p in rows]}


@router.get("/phones/{phone_id}")
def api_phone(
    phone_id: int,
    session: Session = Depends(get_session),
    _: ApiToken = Depends(require_api_token),
):
    phone = session.get(Phone, phone_id)
    if phone is None:
        raise HTTPException(status_code=404, detail="Phone not found")
    info = get_catalog().lookup(phone.model_raw)
    loc = locations.resolve_phone(session, phone)
    stat = session.scalars(
        select(CallStat).where(CallStat.device_name == phone.device_name)
    ).first()
    data = _phone_dict(phone)
    data["replacement_requires"] = info.replacement_requires
    data["location"] = loc.name if loc else None
    data["call_stats"] = (
        {
            "total_calls": stat.total_calls,
            "last_call_at": stat.last_call_at,
            "avg_mos": stat.avg_mos,
        }
        if stat else None
    )
    return data


@router.get("/calls")
def api_calls(
    q: str = "",
    device: str = "",
    date_from: str = "",
    date_to: str = "",
    min_duration: int = 0,
    answered: str = "",
    session: Session = Depends(get_session),
    _: ApiToken = Depends(require_api_token),
):
    results, match = calls.search_calls(
        session, q, device, date_from, date_to, min_duration, answered
    )
    return {
        "count": match,
        "calls": [
            {
                "call_key": c.call_key,
                "start": c.start,
                "calling_number": c.calling_number,
                "final_called": c.final_called,
                "duration": c.duration,
                "answered": c.answered,
                "legs": c.legs,
                "end_cause": c.end_cause,
                "devices": c.devices,
            }
            for c in results
        ],
    }


@router.get("/calls/{call_key}")
def api_call(
    call_key: str,
    session: Session = Depends(get_session),
    _: ApiToken = Depends(require_api_token),
):
    legs = calls.get_call(session, call_key)
    if not legs:
        raise HTTPException(status_code=404, detail="Call not found")
    quality = calls.quality_for_legs(session, legs)
    return {
        "call_key": call_key,
        "legs": [
            {
                "orig_device": leg.orig_device,
                "dest_device": leg.dest_device,
                "calling_number": leg.calling_number,
                "final_called": leg.final_called,
                "orig_time": leg.orig_time,
                "connect_time": leg.connect_time,
                "disconnect_time": leg.disconnect_time,
                "duration": leg.duration,
                "orig_cause": leg.orig_cause,
                "dest_cause": leg.dest_cause,
                "quality": (
                    {
                        "mos": q.mos, "jitter_ms": q.jitter_ms,
                        "latency_ms": q.latency_ms, "loss_pct": q.loss_pct,
                    }
                    if (q := quality.get(leg.dest_leg_id) or quality.get(leg.orig_leg_id))
                    else None
                ),
            }
            for leg in legs
        ],
    }


@router.get("/summary")
def api_summary(
    session: Session = Depends(get_session),
    _: ApiToken = Depends(require_api_token),
):
    return asdict(reports.summary(session))


@router.get("/analytics")
def api_analytics(
    session: Session = Depends(get_session),
    _: ApiToken = Depends(require_api_token),
):
    return analytics.overview(session)


@router.get("/reports/{key}")
def api_report(
    key: str,
    session: Session = Depends(get_session),
    _: ApiToken = Depends(require_api_token),
):
    report = report_templates.build(session, key)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown report")
    return report
