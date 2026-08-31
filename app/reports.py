"""Aggregations behind the dashboard, refresh plan, and PoE views."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .catalog import DEFAULT_FAMILY, get_catalog
from .models import CallStat, Phone, SwitchPoll


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
            func.sum(case((Phone.lifecycle == "eos", 1), else_=0)).label("eos"),
            func.sum(
                case((Phone.registration_status == "Registered", 1), else_=0)
            ).label("registered"),
        )
        .group_by(Phone.site)
        .order_by(func.count().desc())
    ).all()
    out = []
    for site, total, eol, eos, registered in rows:
        eol = int(eol or 0)
        eos = int(eos or 0)
        out.append(
            {
                "site": site or "(no device pool)",
                "total": total,
                "eol": eol,
                "eos": eos,
                "current": max(total - eol - eos, 0),
                "registered": int(registered or 0),
                "reg_pct": round(100 * int(registered or 0) / total) if total else 0,
            }
        )
    return out


def refresh_plan(session: Session, family: str = DEFAULT_FAMILY) -> dict:
    """The quantity lines for a quote, under the chosen replacement family.

    Returns ``{"totals": [...], "total_count": int, "sites": [...]}``. Totals
    collapse across sites; sites carry the per-model breakdown for the bars.
    Anything marked ``excluded`` is left out of the count.
    """
    rows = session.execute(
        select(Phone.site, Phone.model_raw, func.count().label("count"))
        .where(Phone.swap_status != "excluded")
        .group_by(Phone.site, Phone.model_raw)
    ).all()

    catalog = get_catalog()
    totals: dict[str, dict] = {}
    site_map: dict[str, dict[str, int]] = {}
    for site, model_raw, count in rows:
        choice = catalog.replacement_for(model_raw, family)
        if choice is None:
            continue
        entry = totals.setdefault(
            choice.key,
            {
                "key": choice.key,
                "name": choice.name,
                "poe_class": choice.poe_class,
                "poe_watts": choice.poe_watts,
                "count": 0,
            },
        )
        entry["count"] += count
        bucket = site_map.setdefault(site or "(no device pool)", {})
        bucket[choice.key] = bucket.get(choice.key, 0) + count

    totals_list = sorted(totals.values(), key=lambda r: -r["count"])
    total_count = sum(t["count"] for t in totals_list)

    sites = []
    for site, bucket in site_map.items():
        lines = sorted(
            (
                {"name": totals[key]["name"], "count": count}
                for key, count in bucket.items()
            ),
            key=lambda r: -r["count"],
        )
        top = lines[0]["count"] if lines else 1
        sites.append(
            {
                "site": site,
                "total": sum(line["count"] for line in lines),
                "lines": [
                    {**line, "pct": round(100 * line["count"] / top) if top else 0}
                    for line in lines
                ],
            }
        )
    sites.sort(key=lambda s: -s["total"])
    return {"totals": totals_list, "total_count": total_count, "sites": sites}


def mapping_in_use(session: Session, family: str = DEFAULT_FAMILY) -> list[dict]:
    """Every model in service and what it maps to under the chosen family."""
    catalog = get_catalog()
    out = []
    for row in by_model(session):
        info = catalog.lookup(row["model_raw"])
        choice = catalog.replacement_for(row["model_raw"], family)
        out.append(
            {
                "key": row["model_raw"] or row["model_key"],
                "count": row["count"],
                "spec": f"class {info.poe_class} · {info.poe_watts:.2f} W",
                "replacement": choice.name if choice else None,
                "replacement_spec": choice.spec if choice else None,
            }
        )
    return out


def poe_by_switch(session: Session, family: str = DEFAULT_FAMILY) -> list[dict]:
    """Per-switch PoE budget: what phones draw today vs after the swap.

    Budget uses the IEEE class ceiling because that is what the switch
    reserves per port, which is the number that actually runs a closet out
    of power. ``family`` selects the replacement programme; phones marked
    ``excluded`` keep their current draw in the "after" figure.
    """
    rows = session.execute(
        select(
            Phone.switch_name,
            Phone.site,
            Phone.model_raw,
            Phone.swap_status,
            func.count().label("ports"),
            func.sum(func.coalesce(Phone.poe_watts, 0.0)).label("current_w"),
        )
        .where(Phone.switch_name.is_not(None))
        .group_by(
            Phone.switch_name, Phone.site, Phone.model_raw, Phone.swap_status
        )
    ).all()

    catalog = get_catalog()
    switches: dict[tuple, dict] = {}
    for switch, site, model_raw, swap_status, ports, current_w in rows:
        current_w = float(current_w or 0)
        choice = catalog.replacement_for(model_raw, family)
        if choice is not None and swap_status != "excluded":
            future_w = ports * choice.poe_watts
        else:
            future_w = current_w
        entry = switches.setdefault(
            (switch, site),
            {
                "switch_name": switch,
                "site": site or "-",
                "ports": 0,
                "current_w": 0.0,
                "future_w": 0.0,
            },
        )
        entry["ports"] += ports
        entry["current_w"] += current_w
        entry["future_w"] += future_w

    polls = {p.switch_name: p for p in session.scalars(select(SwitchPoll)).all()}

    out = []
    for entry in switches.values():
        current = round(entry["current_w"], 1)
        future = round(entry["future_w"], 1)
        poll = polls.get(entry["switch_name"])
        real_used = (
            round(poll.used_watts, 1)
            if poll and poll.used_watts is not None
            else None
        )
        available = (
            round(poll.available_watts, 1)
            if poll and poll.available_watts is not None
            else None
        )
        out.append(
            {
                "switch_name": entry["switch_name"],
                "site": entry["site"],
                "ports": entry["ports"],
                "current_w": current,
                "future_w": future,
                "delta_w": round(future - current, 1),
                "pct_change": (
                    round(100 * (future - current) / current) if current else 0
                ),
                "real_used": real_used,
                "available": available,
                "headroom_after": (
                    round(available - future, 1) if available is not None else None
                ),
            }
        )
    out.sort(key=lambda r: -r["ports"])
    return out


def clusters(session: Session) -> list[dict]:
    """Phone count per cluster — only interesting once more than one exists."""
    rows = session.execute(
        select(Phone.cluster, func.count().label("total"))
        .group_by(Phone.cluster)
        .order_by(func.count().desc())
    ).all()
    return [
        {"cluster": cluster or "unassigned", "total": total}
        for cluster, total in rows
    ]


def call_activity(session: Session) -> dict:
    """Fleet call-activity summary from CallStat. Powers the "which phones does
    nobody use, so don't replace them" question and a rough fleet MOS."""
    total_calls = session.scalar(
        select(func.coalesce(func.sum(CallStat.total_calls), 0))
    ) or 0
    active = session.scalar(
        select(func.count()).select_from(CallStat).where(CallStat.total_calls > 0)
    ) or 0
    active_names = select(CallStat.device_name).where(CallStat.total_calls > 0)
    total_phones = _count(session)
    unused = session.scalar(
        select(func.count()).select_from(Phone).where(
            Phone.device_name.not_in(active_names)
        )
    ) or 0
    mos_sum = session.scalar(select(func.coalesce(func.sum(CallStat.mos_sum), 0.0))) or 0.0
    mos_count = session.scalar(select(func.coalesce(func.sum(CallStat.mos_count), 0))) or 0
    return {
        "has_data": bool(active),
        "total_calls": int(total_calls),
        "active": int(active),
        "unused": int(unused),
        "total_phones": total_phones,
        "unused_pct": round(100 * unused / total_phones) if total_phones else 0,
        "avg_mos": round(mos_sum / mos_count, 2) if mos_count else None,
    }


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
