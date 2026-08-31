"""Named, ready-to-run reports — the operator's answer sheets.

Each report is (title, subtitle, columns, rows). Rows are plain lists so the
same data drives the on-screen table, the CSV, and the XLSX without rework.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import reports
from .models import Phone

# Order shown on the /reports index.
REPORT_META = [
    ("eol-priority", "Replace first",
     "Phones past end of support — no TAC, no firmware. The order-first list."),
    ("coverage-gaps", "Discovery gaps",
     "Phones missing a serial or a switch port, so you know what to chase."),
    ("by-site", "Fleet by site",
     "Counts and lifecycle split per site, for phasing the rollout."),
    ("by-model", "Fleet by model",
     "What you have and what each maps to, quantities for a quote."),
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
    return "Fleet by site", "Counts and lifecycle per site", columns, data


def _by_model(session: Session):
    columns = ["Model", "Count", "Lifecycle", "Replacement", "Verified"]
    data = [
        [
            m["model_raw"] or m["model_key"], m["count"], m["lifecycle"],
            m["replacement_name"] or "no change", "yes" if m["verified"] else "no",
        ]
        for m in reports.by_model(session)
    ]
    return "Fleet by model", "What you have and what it maps to", columns, data


_BUILDERS = {
    "eol-priority": _eol_priority,
    "coverage-gaps": _coverage_gaps,
    "by-site": _by_site,
    "by-model": _by_model,
}


def build(session: Session, key: str) -> dict | None:
    builder = _BUILDERS.get(key)
    if builder is None:
        return None
    title, subtitle, columns, rows = builder(session)
    return {"key": key, "title": title, "subtitle": subtitle,
            "columns": columns, "rows": rows}
