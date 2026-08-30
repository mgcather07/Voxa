"""Aggregations behind the dashboard, refresh plan, and PoE views."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .catalog import get_catalog
from .models import Phone


@dataclass
class Summary:
    total: int = 0
    registered: int = 0
    unregistered: int = 0
    eol: int = 0
    eos: int = 0
    current: int = 0
    with_serial: int = 0
    with_switch_port: int = 0
    sites: int = 0
    models: int = 0
    swap_counts: dict[str, int] = field(default_factory=dict)

    @property
    def replace_now(self) -> int:
        return self.eol

    @property
    def serial_coverage(self) -> int:
        return round(100 * self.with_serial / self.total) if self.total else 0

    @property
    def port_coverage(self) -> int:
        return round(100 * self.with_switch_port / self.total) if self.total else 0


def _count(session: Session, *conditions) -> int:
    stmt = select(func.count()).select_from(Phone)
    for condition in conditions:
        stmt = stmt.where(condition)
    return session.scalar(stmt) or 0


def summary(session: Session) -> Summary:
    s = Summary()
    s.total = _count(session)
    s.registered = _count(session, Phone.registration_status == "Registered")
    s.unregistered = s.total - s.registered
    s.eol = _count(session, Phone.lifecycle == "eol")
    s.eos = _count(session, Phone.lifecycle == "eos")
    s.current = _count(session, Phone.lifecycle == "current")
    s.with_serial = _count(session, Phone.serial_number.is_not(None))
    s.with_switch_port = _count(session, Phone.switch_port.is_not(None))
    s.sites = session.scalar(
        select(func.count(func.distinct(Phone.site))).where(Phone.site.is_not(None))
    ) or 0
    s.models = session.scalar(
        select(func.count(func.distinct(Phone.model_key)))
    ) or 0
    s.swap_counts = {
        status: count
        for status, count in session.execute(
            select(Phone.swap_status, func.count()).group_by(Phone.swap_status)
        ).all()
    }
    return s


def by_model(session: Session) -> list[dict]:
    """Model breakdown with lifecycle and recommended replacement."""
    rows = session.execute(
        select(
            Phone.model_key,
            Phone.model_raw,
            Phone.lifecycle,
            Phone.replacement_key,
            Phone.replacement_name,
            func.count().label("count"),
        )
        .group_by(
            Phone.model_key,
            Phone.model_raw,
            Phone.lifecycle,
            Phone.replacement_key,
            Phone.replacement_name,
        )
        .order_by(func.count().desc())
    ).all()

    catalog = get_catalog()
    merged: dict[str, dict] = {}
    for model_key, model_raw, lifecycle, rep_key, rep_name, count in rows:
        key = model_key or "unknown"
        entry = merged.setdefault(
            key,
            {
                "model_key": key,
                "model_raw": model_raw,
                "lifecycle": lifecycle or "unknown",
                "replacement_key": rep_key,
                "replacement_name": rep_name,
                "count": 0,
                "verified": catalog.lookup(model_raw).verified,
            },
        )
        entry["count"] += count
    return sorted(merged.values(), key=lambda r: -r["count"])


def by_site(session: Session) -> list[dict]:
    rows = session.execute(
        select(
            Phone.site,
            func.count().label("total"),
            func.sum(case((Phone.lifecycle == "eol", 1), else_=0)).label("eol"),
            func.sum(
                case((Phone.registration_status == "Registered", 1), else_=0)
            ).label("registered"),
        )
        .group_by(Phone.site)
        .order_by(func.count().desc())
    ).all()
    return [
        {
            "site": site or "(no device pool)",
            "total": total,
            "eol": int(eol or 0),
            "registered": int(registered or 0),
        }
        for site, total, eol, registered in rows
    ]


def replacement_plan(session: Session) -> list[dict]:
    """How many of each replacement model to buy, grouped by site."""
    rows = session.execute(
        select(
            Phone.site,
            Phone.replacement_key,
            Phone.replacement_name,
            func.count().label("count"),
        )
        .where(Phone.replacement_key.is_not(None))
        .where(Phone.swap_status != "excluded")
        .group_by(Phone.site, Phone.replacement_key, Phone.replacement_name)
        .order_by(Phone.site, func.count().desc())
    ).all()
    return [
        {
            "site": site or "(no device pool)",
            "replacement_key": rep_key,
            "replacement_name": rep_name,
            "count": count,
        }
        for site, rep_key, rep_name, count in rows
    ]


def poe_by_switch(session: Session) -> list[dict]:
    """Per-switch PoE budget: what phones draw today vs after the swap.

    Budget uses the IEEE class ceiling because that is what the switch
    reserves per port, which is the number that actually runs a closet out
    of power.
    """
    rows = session.execute(
        select(
            Phone.switch_name,
            Phone.site,
            func.count().label("ports"),
            func.sum(func.coalesce(Phone.poe_watts, 0.0)).label("current_w"),
            func.sum(
                func.coalesce(Phone.replacement_poe_watts, Phone.poe_watts, 0.0)
            ).label("future_w"),
        )
        .where(Phone.switch_name.is_not(None))
        .group_by(Phone.switch_name, Phone.site)
        .order_by(func.count().desc())
    ).all()

    out = []
    for switch, site, ports, current_w, future_w in rows:
        current = round(float(current_w or 0), 1)
        future = round(float(future_w or 0), 1)
        out.append(
            {
                "switch_name": switch,
                "site": site or "-",
                "ports": ports,
                "current_w": current,
                "future_w": future,
                "delta_w": round(future - current, 1),
                "pct_change": (
                    round(100 * (future - current) / current) if current else 0
                ),
            }
        )
    return out


def unverified_models(session: Session) -> list[str]:
    """Models in use whose catalog entry has not been fact-checked yet."""
    catalog = get_catalog()
    keys = session.scalars(
        select(func.distinct(Phone.model_raw)).where(Phone.model_raw.is_not(None))
    ).all()
    return sorted(
        {
            catalog.lookup(raw).key
            for raw in keys
            if not catalog.lookup(raw).verified
        }
    )
