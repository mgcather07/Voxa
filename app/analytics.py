"""Call analytics aggregations over stored CDR/CMR.

Python-side aggregation (the record volume is modest and it stays portable
across SQLite/Postgres) turning raw legs into the numbers an operator asks for:
volume over time, busy hour, top talkers, quality distribution, disconnect
causes, and missed calls.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mos as mos_lib
from .calls import cause_label
from .models import CallQuality, CallRecord, utcnow


def _legs(session: Session):
    return session.execute(
        select(
            CallRecord.orig_time,
            CallRecord.calling_number,
            CallRecord.final_called,
            CallRecord.duration,
            CallRecord.dest_cause,
            CallRecord.orig_device,
            CallRecord.dest_device,
        )
    ).all()


def _is_gateway(device: str | None) -> bool:
    """A device that isn't a phone (SEP…) and isn't empty — a gateway/trunk."""
    return bool(device) and not device.startswith("SEP")


def quality_issues(session: Session, limit: int = 12) -> list[dict]:
    """Worst-MOS legs, each linked back to its call for a one-click trace."""
    bad = session.execute(
        select(CallQuality).where(CallQuality.mos.is_not(None),
                                  CallQuality.mos < mos_lib.PROBLEM_MAX)
        .order_by(CallQuality.mos.asc()).limit(limit)
    ).scalars().all()
    out = []
    for cq in bad:
        rec = session.scalars(
            select(CallRecord).where(
                (CallRecord.orig_leg_id == cq.leg_id)
                | (CallRecord.dest_leg_id == cq.leg_id)
            ).limit(1)
        ).first()
        out.append({
            "device": cq.device,
            "mos": cq.mos,
            "jitter_ms": cq.jitter_ms,
            "latency_ms": cq.latency_ms,
            "loss_pct": cq.loss_pct,
            "call_key": rec.call_key if rec else None,
            "when": rec.orig_time if rec else None,
        })
    return out


def overview(session: Session) -> dict:
    rows = _legs(session)
    now = utcnow()
    days = 14

    by_day = {(now.date() - timedelta(days=i)): {"total": 0, "answered": 0}
              for i in range(days)}
    by_hour = {h: 0 for h in range(24)}
    talkers: Counter = Counter()
    causes: Counter = Counter()
    gw_calls: Counter = Counter()
    gw_seconds: Counter = Counter()
    missed = 0
    total = 0

    for orig_time, calling, called, duration, dest_cause, orig_dev, dest_dev in rows:
        total += 1
        answered = (duration or 0) > 0
        if not answered:
            missed += 1
            if dest_cause is not None:
                causes[int(dest_cause)] += 1
        if calling:
            talkers[calling] += 1
        for dev in (orig_dev, dest_dev):
            if _is_gateway(dev):
                gw_calls[dev] += 1
                gw_seconds[dev] += duration or 0
        if orig_time is not None:
            d = orig_time.date()
            if d in by_day:
                by_day[d]["total"] += 1
                if answered:
                    by_day[d]["answered"] += 1
            by_hour[orig_time.hour] += 1

    day_series = [
        {"day": d, "total": v["total"], "answered": v["answered"]}
        for d, v in sorted(by_day.items())
    ]
    hour_series = [{"hour": h, "total": by_hour[h]} for h in range(24)]
    busy_hour = max(hour_series, key=lambda r: r["total"]) if total else None

    # Quality distribution from CMR MOS values.
    mos_values = [
        m for (m,) in session.execute(
            select(CallQuality.mos).where(CallQuality.mos.is_not(None))
        ).all()
    ]
    # Distribution across the five shared MOS bands (best → worst).
    band_counts = {b.key: 0 for b in mos_lib.BANDS}
    for m in mos_values:
        band_counts[mos_lib.band_for(m).key] += 1
    quality = [
        {
            "label": b.label,
            "range": b.range_text,
            "color": b.color,
            "count": band_counts[b.key],
        }
        for b in mos_lib.BANDS_DESC
    ]
    counts = list(band_counts.values())
    avg_mos = round(sum(mos_values) / len(mos_values), 2) if mos_values else None

    return {
        "total_calls": total,
        "answered_pct": round(100 * (total - missed) / total) if total else 0,
        "missed": missed,
        "missed_pct": round(100 * missed / total) if total else 0,
        "avg_mos": avg_mos,
        "day_series": day_series,
        "day_max": max((d["total"] for d in day_series), default=1) or 1,
        "hour_series": hour_series,
        "hour_max": max((h["total"] for h in hour_series), default=1) or 1,
        "busy_hour": busy_hour,
        "top_talkers": [
            {"number": num, "calls": n} for num, n in talkers.most_common(10)
        ],
        "talker_max": talkers.most_common(1)[0][1] if talkers else 1,
        "quality": quality,
        "quality_total": sum(counts) or 1,
        "causes": [
            {"cause": code, "label": cause_label(code), "count": n}
            for code, n in causes.most_common(8)
        ],
        "gateways": [
            {"name": gw, "calls": n, "minutes": round(gw_seconds[gw] / 60)}
            for gw, n in gw_calls.most_common(10)
        ],
        "gateway_max": gw_calls.most_common(1)[0][1] if gw_calls else 1,
        "quality_issues": quality_issues(session),
        "has_data": total > 0,
    }
