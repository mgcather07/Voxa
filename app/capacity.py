"""Concurrency and capacity analytics from CDR call intervals.

How many calls are up at the same time — the number that sizes trunks and CUCM
session capacity. Computed with a sweep line over each call's active window
[connect_or_orig .. disconnect]. Read-only; no new data source.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CallRecord, TrunkCapacity


def _intervals(session: Session, gateway: str | None = None):
    """(start, end, is_gateway_leg) for legs that were actually up."""
    stmt = select(
        CallRecord.orig_time,
        CallRecord.connect_time,
        CallRecord.disconnect_time,
        CallRecord.duration,
        CallRecord.orig_device,
        CallRecord.dest_device,
    ).where(CallRecord.duration > 0)
    rows = session.execute(stmt).all()
    out = []
    for orig, connect, disconnect, duration, od, dd in rows:
        start = connect or orig
        end = disconnect or (start + timedelta(seconds=duration or 0) if start else None)
        if not start or not end or end <= start:
            continue
        if gateway is not None and gateway not in (od, dd):
            continue
        out.append((start, end))
    return out


def _peak_concurrency(intervals) -> tuple[int, object]:
    """Sweep line: max overlapping intervals and when it occurred."""
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], -e[1]))  # ends after starts at same instant
    cur = peak = 0
    peak_at = None
    for t, delta in events:
        cur += delta
        if cur > peak:
            peak = cur
            peak_at = t
    return peak, peak_at


def _erlangs(intervals, span_seconds: float) -> float:
    total = sum((end - start).total_seconds() for start, end in intervals)
    return round(total / span_seconds, 1) if span_seconds else 0.0


def _bhca(intervals) -> int:
    """Busiest 60-minute count of call starts (sliding window)."""
    starts = sorted(s for s, _ in intervals)
    if not starts:
        return 0
    window = timedelta(hours=1)
    best = 0
    j = 0
    for i in range(len(starts)):
        while starts[i] - starts[j] > window:
            j += 1
        best = max(best, i - j + 1)
    return best


def _series(intervals, buckets: int = 48):
    """Peak concurrency per time bucket across the data range."""
    if not intervals:
        return []
    lo = min(s for s, _ in intervals)
    hi = max(e for _, e in intervals)
    span = (hi - lo).total_seconds()
    if span <= 0:
        return []
    step = span / buckets
    # For each bucket, count intervals overlapping its midpoint window peak.
    edges = [lo + timedelta(seconds=step * i) for i in range(buckets + 1)]
    out = []
    for i in range(buckets):
        b_lo, b_hi = edges[i], edges[i + 1]
        overlap = [
            (max(s, b_lo), min(e, b_hi))
            for s, e in intervals
            if s < b_hi and e > b_lo
        ]
        peak, _ = _peak_concurrency(overlap)
        out.append({"start": b_lo, "peak": peak})
    return out


def capacities(session: Session) -> dict[str, int]:
    return {
        t.gateway_name: t.channels
        for t in session.scalars(select(TrunkCapacity)).all()
    }


def overview(session: Session) -> dict:
    intervals = _intervals(session)
    if not intervals:
        return {"has_data": False}
    lo = min(s for s, _ in intervals)
    hi = max(e for _, e in intervals)
    span = (hi - lo).total_seconds()

    peak, peak_at = _peak_concurrency(intervals)
    series = _series(intervals)
    smax = max((b["peak"] for b in series), default=1) or 1

    # Per-gateway capacity table.
    caps = capacities(session)
    gateways = sorted(
        {
            d for (d,) in session.execute(
                select(CallRecord.dest_device).where(
                    CallRecord.dest_device.is_not(None)
                ).distinct()
            ).all()
            if d and not d.startswith("SEP")
        }
    )
    gw_rows = []
    for gw in gateways:
        gi = _intervals(session, gateway=gw)
        gpeak, _ = _peak_concurrency(gi)
        erl = _erlangs(gi, span)
        channels = caps.get(gw, 0)
        util = round(100 * gpeak / channels) if channels else None
        gw_rows.append({
            "name": gw, "peak": gpeak, "erlangs": erl,
            "channels": channels, "util": util,
            "minutes": round(sum((e - s).total_seconds() for s, e in gi) / 60),
        })
    gw_rows.sort(key=lambda r: -r["peak"])

    return {
        "has_data": True,
        "peak": peak,
        "peak_at": peak_at,
        "erlangs": _erlangs(intervals, span),
        "bhca": _bhca(intervals),
        "series": series,
        "series_max": smax,
        "gateways": gw_rows,
    }
