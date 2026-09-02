"""Call search and cradle-to-grave assembly over stored CDR/CMR records.

CDR legs sharing (call_mgr_id, call_id) are one call. Search returns one row per
call; the detail view (see routes) stitches every leg together with its CMR
quality and builds a ladder of the signalling flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _sortable(dt: datetime | None) -> datetime:
    """Naive datetime for ordering, tolerant of None and tz-aware values."""
    if dt is None:
        return datetime.min
    return dt.replace(tzinfo=None)

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import mos as mos_lib
from .models import CallQuality, CallRecord

# Q.850 disconnect causes — the ones an operator actually sees.
CAUSE_LABELS = {
    0: "Unallocated number",
    1: "Unallocated number",
    16: "Normal clearing",
    17: "User busy",
    18: "No user responding",
    19: "No answer",
    20: "Subscriber absent",
    21: "Call rejected",
    22: "Number changed",
    27: "Destination out of order",
    28: "Invalid number",
    31: "Normal, unspecified",
    34: "No circuit available",
    38: "Network out of order",
    41: "Temporary failure",
    42: "Switching equipment congestion",
    44: "Requested channel unavailable",
    47: "Resource unavailable",
    63: "Service not available",
    65: "Bearer capability not implemented",
    88: "Incompatible destination",
    102: "Recovery on timer expiry",
    126: "Call split",
    127: "Interworking, unspecified",
}


def cause_label(code) -> str:
    if code is None:
        return "—"
    return CAUSE_LABELS.get(int(code), f"Cause {code}")


@dataclass
class CallSummary:
    call_key: str
    start: datetime | None
    calling_number: str | None
    final_called: str | None
    duration: int
    answered: bool
    legs: int
    end_cause: int | None
    devices: list[str] = field(default_factory=list)
    # Representative MOS = the worst measured leg (what the caller actually
    # heard). None when no leg of the call carries a CMR quality record.
    mos: float | None = None


def _call_mos(legs: list[CallRecord], qmap: dict[int, CallQuality]) -> float | None:
    """Worst MOS across a call's legs, or None if no leg was measured."""
    scores = []
    for leg in legs:
        for lid in (leg.orig_leg_id, leg.dest_leg_id):
            q = qmap.get(lid) if lid else None
            if q is not None and q.mos is not None:
                scores.append(q.mos)
    return round(min(scores), 2) if scores else None


def _summarize(
    call_key: str,
    legs: list[CallRecord],
    qmap: dict[int, CallQuality] | None = None,
) -> CallSummary:
    legs = sorted(legs, key=lambda r: _sortable(r.orig_time))
    first = legs[0]
    last = legs[-1]
    devices = []
    for leg in legs:
        for d in (leg.orig_device, leg.dest_device):
            if d and d not in devices:
                devices.append(d)
    return CallSummary(
        call_key=call_key,
        start=first.orig_time,
        calling_number=first.calling_number,
        final_called=last.final_called or last.original_called,
        duration=max((leg.duration or 0) for leg in legs),
        answered=any(leg.answered for leg in legs),
        legs=len(legs),
        end_cause=last.dest_cause if last.dest_cause is not None else last.orig_cause,
        devices=devices,
        mos=_call_mos(legs, qmap or {}),
    )


def _quality_by_leg(
    session: Session, legs: list[CallRecord]
) -> dict[int, CallQuality]:
    """One CMR lookup for a whole batch of legs: {leg_id: CallQuality}."""
    leg_ids = {
        lid
        for leg in legs
        for lid in (leg.orig_leg_id, leg.dest_leg_id)
        if lid
    }
    if not leg_ids:
        return {}
    rows = session.scalars(
        select(CallQuality).where(CallQuality.leg_id.in_(leg_ids))
    ).all()
    return {r.leg_id: r for r in rows}


def search_calls(
    session: Session,
    q: str = "",
    device: str = "",
    date_from: str = "",
    date_to: str = "",
    min_duration: int = 0,
    answered: str = "",
    mos_band: str = "",
    sort: str = "",
    limit: int = 200,
) -> tuple[list[CallSummary], int]:
    """Return (call summaries, distinct-call match count). Filters on legs, then
    groups to calls, attaches each call's worst-leg MOS, and optionally filters
    by quality band / sorts by MOS.

    ``mos_band`` is a band key ("excellent"…"bad"), ``"problem"`` for any call
    below the problem threshold, or "" for all. ``sort`` is "mos_asc",
    "mos_desc", or "" (most recent first).
    """
    stmt = select(CallRecord)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                CallRecord.calling_number.ilike(needle),
                CallRecord.original_called.ilike(needle),
                CallRecord.final_called.ilike(needle),
                CallRecord.orig_device.ilike(needle),
                CallRecord.dest_device.ilike(needle),
            )
        )
    if device:
        stmt = stmt.where(
            or_(CallRecord.orig_device == device, CallRecord.dest_device == device)
        )
    if date_from:
        stmt = stmt.where(CallRecord.orig_time >= _parse_date(date_from))
    if date_to:
        stmt = stmt.where(CallRecord.orig_time <= _parse_date(date_to, end=True))
    if min_duration:
        stmt = stmt.where(CallRecord.duration >= min_duration)
    if answered == "yes":
        stmt = stmt.where(CallRecord.duration > 0)
    elif answered == "no":
        stmt = stmt.where(CallRecord.duration == 0)

    legs = session.scalars(stmt.order_by(CallRecord.orig_time.desc())).all()

    grouped: dict[str, list[CallRecord]] = {}
    for leg in legs:
        grouped.setdefault(leg.call_key, []).append(leg)

    qmap = _quality_by_leg(session, list(legs))
    summaries = [_summarize(k, v, qmap) for k, v in grouped.items()]

    # Quality band filter. A call with no measured MOS never matches a band
    # (it is not silently treated as 0).
    if mos_band == "problem":
        summaries = [s for s in summaries if s.mos is not None
                     and s.mos < mos_lib.PROBLEM_MAX]
    elif mos_band:
        summaries = [s for s in summaries
                     if s.mos is not None
                     and mos_lib.band_for(s.mos).key == mos_band]

    if sort == "mos_asc":
        summaries.sort(key=lambda s: (s.mos is None, s.mos if s.mos is not None else 0))
    elif sort == "mos_desc":
        summaries.sort(key=lambda s: (s.mos is None, -(s.mos or 0)))
    else:
        summaries.sort(key=lambda s: _sortable(s.start), reverse=True)

    return summaries[:limit], len(summaries)


def get_call(session: Session, call_key: str) -> list[CallRecord]:
    try:
        mgr_id, call_id = (int(x) for x in call_key.split("-", 1))
    except ValueError:
        return []
    legs = session.scalars(
        select(CallRecord)
        .where(CallRecord.call_mgr_id == mgr_id, CallRecord.call_id == call_id)
        .order_by(CallRecord.orig_time.asc(), CallRecord.id.asc())
    ).all()
    return list(legs)


def quality_for_legs(session: Session, legs: list[CallRecord]) -> dict[int, CallQuality]:
    """Map leg identifier -> CallQuality for the legs in a call."""
    leg_ids = set()
    for leg in legs:
        for lid in (leg.orig_leg_id, leg.dest_leg_id):
            if lid:
                leg_ids.add(lid)
    if not leg_ids:
        return {}
    rows = session.scalars(
        select(CallQuality).where(CallQuality.leg_id.in_(leg_ids))
    ).all()
    return {r.leg_id: r for r in rows}


def call_quality_summary(
    legs: list[CallRecord], quality: dict[int, CallQuality]
) -> dict | None:
    """The headline call-quality view for one call: the worst measured leg (the
    caller's actual experience), its telemetry, and a deterministic likely
    contributor. Returns None when no leg carries a MOS. Only reports telemetry
    that was actually collected — never invents codec/concealment fields."""
    measured = [q for q in quality.values() if q.mos is not None]
    if not measured:
        return None
    worst = min(measured, key=lambda q: q.mos)
    return {
        "mos": round(worst.mos, 2),
        "rating": mos_lib.rating(worst.mos),
        "device": worst.device,
        "directory_number": worst.directory_number,
        "jitter_ms": worst.jitter_ms,
        "latency_ms": worst.latency_ms,
        "loss_pct": worst.loss_pct,
        "packets_lost": worst.packets_lost,
        "packets_sent": worst.packets_sent,
        "legs_measured": len(measured),
        "likely_issue": mos_lib.likely_issue(
            worst.loss_pct, worst.jitter_ms, worst.latency_ms
        ),
    }


def _dur(seconds) -> str:
    """A compact call/leg length: 8m 3s, or 45s."""
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def build_ladder(legs: list[CallRecord], device_info: dict | None = None,
                 node_desc: dict | None = None, tz=None) -> dict | None:
    """Build a SIP-style ladder (sequence diagram) of a call's signalling.

    Lifelines are the parties plus the CallManager they route through; arrows
    are SETUP / ANSWER / RELEASE events ordered in time. Coordinates are
    computed here so the template just draws them.
    """
    if not legs:
        return None
    legs = sorted(legs, key=lambda r: _sortable(r.orig_time))
    mgr = legs[0].call_mgr_id
    cucm_key = f"__cucm__{mgr}"

    device_info = device_info or {}
    node_desc = node_desc or {}
    # Best IP per device: prefer the address recorded in the CDR leg, fall back
    # to the phone's current registration IP from inventory.
    cdr_ip: dict[str, str] = {}
    for leg in legs:
        if leg.orig_device and leg.orig_ip:
            cdr_ip.setdefault(leg.orig_device, leg.orig_ip)
        if leg.dest_device and leg.dest_ip:
            cdr_ip.setdefault(leg.dest_device, leg.dest_ip)

    def device_ip(dev) -> str:
        if not dev:
            return ""
        return cdr_ip.get(dev) or (device_info.get(dev) or {}).get("ip") or ""

    # The CallManager node these phones registered to, for the CUCM lifeline.
    cm_node = ""
    for leg in legs:
        for dev in (leg.orig_device, leg.dest_device):
            node = (device_info.get(dev) or {}).get("cm_node")
            if node:
                cm_node = node
                break
        if cm_node:
            break

    order: list[str] = []
    meta: dict[str, dict] = {}

    def party(device, number, cucm=False) -> str:
        key = cucm_key if cucm else (device or number or "?")
        if key not in meta:
            if cucm:
                friendly = node_desc.get(cm_node) if cm_node else None
                # Prefer the node's human name; drop its IP/hostname to line 3.
                meta[key] = {
                    "label": f"CUCM {mgr}",
                    "sub": friendly or cm_node or "CallManager",
                    "ip": cm_node if friendly else "",
                }
            else:
                meta[key] = {
                    "label": number or device or "?",
                    "sub": device if (device and number) else
                    ("" if device else "external"),
                    "ip": device_ip(device),
                }
            order.append(key)
        return key

    events: list[dict] = []

    def add(t, frm, to, label, kind):
        if t is None:
            return
        events.append({"t": t, "frm": frm, "to": to, "label": label, "kind": kind})

    for leg in legs:
        o = party(leg.orig_device, leg.calling_number)
        cm = party(None, None, cucm=True)
        d = party(leg.dest_device, leg.final_called or leg.original_called)
        add(leg.orig_time, o, cm, "INVITE", "setup")
        add(leg.orig_time, cm, d, "INVITE", "setup")
        if leg.answered:
            add(leg.connect_time, d, cm, "200 OK", "answer")
            add(leg.connect_time, cm, o, "200 OK", "answer")
            rel = f"BYE · {cause_label(leg.dest_cause or leg.orig_cause)}"
            if _dur(leg.duration):
                rel += f" · {_dur(leg.duration)}"
            add(leg.disconnect_time, o, cm, rel, "release")
            add(leg.disconnect_time, cm, d, "BYE", "release")
        else:
            lbl = f"CANCEL · {cause_label(leg.dest_cause or leg.orig_cause)}"
            add(leg.disconnect_time, cm, d, lbl, "release")
            add(leg.disconnect_time, d, cm, "486 / release", "release")

    # Put CUCM in the middle of the lifelines for a readable ladder.
    if cucm_key in order:
        order.remove(cucm_key)
        order.insert(min(1, len(order)), cucm_key)

    events.sort(key=lambda e: (_sortable(e["t"]), 0))
    events = events[:80]

    # Layout.
    left_gutter = 96
    col_w = 182
    x0 = left_gutter + 60
    top = 82
    row_h = 34
    xs = {key: x0 + i * col_w for i, key in enumerate(order)}
    width = x0 + (len(order) - 1) * col_w + 80
    height = top + len(events) * row_h + 40

    participants = [
        {"x": xs[key], "label": meta[key]["label"], "sub": meta[key]["sub"],
         "ip": meta[key].get("ip", ""), "cucm": key == cucm_key}
        for key in order
    ]
    def _fmt(t):
        if not t:
            return ""
        if tz is not None:
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            t = t.astimezone(tz)
        return t.strftime("%I:%M:%S %p").lstrip("0")

    arrows = []
    for i, e in enumerate(events):
        y = top + i * row_h
        x1, x2 = xs[e["frm"]], xs[e["to"]]
        arrows.append({
            "x1": x1, "x2": x2, "y": y,
            "mid": (x1 + x2) / 2,
            "label": e["label"], "kind": e["kind"],
            "dir": 1 if x2 >= x1 else -1,
            "time": _fmt(e["t"]),
        })

    return {
        "width": width, "height": height, "top": top - 24,
        "bottom": height - 30, "gutter": left_gutter,
        "participants": participants, "arrows": arrows,
    }


def _parse_date(value: str, end: bool = False) -> datetime:
    """Parse a YYYY-MM-DD (or datetime) filter value; naive is fine for compare."""
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if end and fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    return datetime.min
