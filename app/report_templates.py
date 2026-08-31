"""Named, ready-to-run reports — the operator's answer sheets.

Each report is (title, subtitle, columns, rows). Rows are plain lists so the
same data drives the on-screen table, the CSV, and the XLSX without rework.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import reports
from .calls import cause_label
from .models import CallQuality, CallRecord, CallStat, Phone

# Order shown on the /reports index.
REPORT_META = [
    ("eol-priority", "Replace first",
     "Phones past end of support — no TAC, no firmware. The order-first list."),
    ("unused-phones", "Unused phones",
     "No calls recorded — candidates to retire rather than replace."),
    ("coverage-gaps", "Discovery gaps",
     "Phones missing a serial or a switch port, so you know what to chase."),
    ("by-site", "Inventory by site",
     "Counts and lifecycle split per site, for phasing the rollout."),
    ("by-model", "Inventory by model",
     "What you have and what each maps to, quantities for a quote."),
    ("missed-calls", "Missed calls",
     "Unanswered calls with the disconnect cause — from CDR."),
    ("poor-quality", "Poor-quality calls",
     "Call legs with a MOS below 3.6 — the ones users complain about."),
    ("top-talkers", "Top talkers",
     "Busiest extensions by call volume."),
    ("gateway-summary", "Gateways & trunks",
     "External call volume and minutes per PSTN gateway / SIP trunk."),
]


def _eol_priority(session: Session):
    rows = session.scalars(
        select(Phone)
        .where(Phone.lifecycle == "eol")
        .order_by(Phone.site, Phone.device_name)
    ).all()
    columns = ["Device", "DN", "Model", "Site", "Switch", "Port", "Replace with"]
    data = [
        [
            p.device_name, p.directory_number or "", p.model_raw or p.model_key or "",
            p.site or "", p.switch_name or "", p.switch_port or "",
            p.replacement_name or "no change",
        ]
        for p in rows
    ]
    return "Replace first", "Past end of support", columns, data


def _coverage_gaps(session: Session):
    rows = session.scalars(
        select(Phone)
        .where((Phone.serial_number.is_(None)) | (Phone.switch_name.is_(None)))
        .order_by(Phone.site, Phone.device_name)
    ).all()
    columns = ["Device", "Site", "Registration", "IP", "Missing"]
    data = []
    for p in rows:
        missing = []
        if p.serial_number is None:
            missing.append("serial")
        if p.switch_name is None:
            missing.append("switch port")
        data.append([
            p.device_name, p.site or "", p.registration_status or "",
            p.ip_address or "", ", ".join(missing),
        ])
    return "Discovery gaps", "Missing serial or switch port", columns, data


def _by_site(session: Session):
    columns = ["Site", "Phones", "Past support", "End of sale", "Current", "Registered %"]
    data = [
        [s["site"], s["total"], s["eol"], s["eos"], s["current"], s["reg_pct"]]
        for s in reports.by_site(session)
    ]
    return "Inventory by site", "Counts and lifecycle per site", columns, data


def _by_model(session: Session):
    columns = ["Model", "Count", "Lifecycle", "Replacement", "Verified"]
    data = [
        [
            m["model_raw"] or m["model_key"], m["count"], m["lifecycle"],
            m["replacement_name"] or "no change", "yes" if m["verified"] else "no",
        ]
        for m in reports.by_model(session)
    ]
    return "Inventory by model", "What you have and what it maps to", columns, data


def _unused_phones(session: Session):
    active = select(CallStat.device_name).where(CallStat.total_calls > 0)
    rows = session.scalars(
        select(Phone)
        .where(Phone.device_name.not_in(active))
        .order_by(Phone.site, Phone.device_name)
    ).all()
    columns = ["Device", "DN", "Model", "Site", "Lifecycle"]
    data = [
        [
            p.device_name, p.directory_number or "",
            p.model_raw or p.model_key or "", p.site or "", p.lifecycle or "",
        ]
        for p in rows
    ]
    return "Unused phones", "No calls recorded — retire rather than replace", columns, data


def _missed_calls(session: Session):
    rows = session.scalars(
        select(CallRecord).where(CallRecord.duration == 0)
        .order_by(CallRecord.orig_time.desc())
    ).all()
    columns = ["When", "Calling", "Called", "Orig device", "Cause"]
    data = [
        [
            r.orig_time.strftime("%b %d, %Y %I:%M:%S %p") if r.orig_time else "",
            r.calling_number or "", r.final_called or r.original_called or "",
            r.orig_device or "",
            cause_label(r.dest_cause if r.dest_cause is not None else r.orig_cause),
        ]
        for r in rows
    ]
    return "Missed calls", "Unanswered calls, from CDR", columns, data


def _poor_quality(session: Session):
    bad = session.scalars(
        select(CallQuality).where(CallQuality.mos.is_not(None), CallQuality.mos < 3.6)
        .order_by(CallQuality.mos.asc())
    ).all()
    columns = ["Device", "MOS", "Jitter ms", "Latency ms", "Loss %", "Ext"]
    data = [
        [cq.device or "", cq.mos, cq.jitter_ms, cq.latency_ms, cq.loss_pct,
         cq.directory_number or ""]
        for cq in bad
    ]
    return "Poor-quality calls", "Legs with MOS < 3.6", columns, data


def _top_talkers(session: Session):
    counter: Counter = Counter()
    for (num,) in session.execute(
        select(CallRecord.calling_number).where(CallRecord.calling_number.is_not(None))
    ).all():
        counter[num] += 1
    columns = ["Extension", "Calls"]
    data = [[num, n] for num, n in counter.most_common(100)]
    return "Top talkers", "Busiest extensions by call volume", columns, data


def _gateway_summary(session: Session):
    calls_ct: Counter = Counter()
    secs: Counter = Counter()
    for orig, dest, dur in session.execute(
        select(CallRecord.orig_device, CallRecord.dest_device, CallRecord.duration)
    ).all():
        for dev in (orig, dest):
            if dev and not dev.startswith("SEP"):
                calls_ct[dev] += 1
                secs[dev] += dur or 0
    columns = ["Gateway / trunk", "Calls", "Minutes"]
    data = [
        [gw, n, round(secs[gw] / 60)] for gw, n in calls_ct.most_common()
    ]
    return "Gateways & trunks", "External call volume per gateway", columns, data


_BUILDERS = {
    "eol-priority": _eol_priority,
    "unused-phones": _unused_phones,
    "coverage-gaps": _coverage_gaps,
    "by-site": _by_site,
    "by-model": _by_model,
    "missed-calls": _missed_calls,
    "poor-quality": _poor_quality,
    "top-talkers": _top_talkers,
    "gateway-summary": _gateway_summary,
}


def build(session: Session, key: str) -> dict | None:
    builder = _BUILDERS.get(key)
    if builder is None:
        return None
    title, subtitle, columns, rows = builder(session)
    return {"key": key, "title": title, "subtitle": subtitle,
            "columns": columns, "rows": rows}
