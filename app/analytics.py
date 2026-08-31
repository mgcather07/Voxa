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
        )
    ).all()


def overview(session: Session) -> dict:
    rows = _legs(session)
    now = utcnow()
    days = 14

    by_day = {(now.date() - timedelta(days=i)): {"total": 0, "answered": 0}
              for i in range(days)}
    by_hour = {h: 0 for h in range(24)}
    talkers: Counter = Counter()
    causes: Counter = Counter()
    missed = 0
    total = 0

    for orig_time, calling, called, duration, dest_cause in rows:
        total += 1
        answered = (duration or 0) > 0
        if not answered:
            missed += 1
            if dest_cause is not None:
                causes[int(dest_cause)] += 1
        if calling:
            talkers[calling] += 1
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
    buckets = [
        ("Excellent", "≥ 4.3", "var(--green)", 0),
        ("Good", "4.0–4.3", "var(--mint)", 0),
        ("Fair", "3.6–4.0", "var(--orange)", 0),
        ("Poor", "< 3.6", "var(--red)", 0),
    ]
    counts = [0, 0, 0, 0]
    for m in mos_values:
        if m >= 4.3:
            counts[0] += 1
        elif m >= 4.0:
            counts[1] += 1
        elif m >= 3.6:
            counts[2] += 1
        else:
            counts[3] += 1
    quality = [
        {"label": b[0], "range": b[1], "color": b[2], "count": counts[i]}
        for i, b in enumerate(buckets)
    ]
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
        "has_data": total > 0,
    }
