"""Ingest CUCM CDR/CMR files into per-device call aggregates.

CUCM's Billing Application Server pushes CDR (call detail) and CMR (call
management / quality) CSV files to an SFTP endpoint. Land them in a directory
(the OS/SFTP does that) and point Voxa at it; this reads the directory and
folds each file into `CallStat` rows. No SFTP client lives in the app — the
transfer is the OS's job, same principle as scheduled collection.

The CDR/CMR CSV layout: line 1 is a header of field names, line 2 is a row of
column *types* (INTEGER/VARCHAR…) which we skip, then data rows. Field names
vary a little across CUCM versions, so lookups are case-insensitive and try a
few candidates per field.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CallStat

log = logging.getLogger(__name__)


def _pick(row: dict, *names: str):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def _rows(path: Path):
    """Yield dict rows from a CUCM CSV, keys lower-cased, type row skipped."""
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return
        for i, raw in enumerate(reader):
            if not raw:
                continue
            # Skip CUCM's type row (all cells look like INTEGER/VARCHAR).
            if i == 0 and all(
                c.strip().upper() in {"INTEGER", "VARCHAR", "FLOAT", "BOOLEAN"}
                or c.strip().upper().startswith("VARCHAR")
                for c in raw
                if c.strip()
            ):
                continue
            yield dict(zip(header, raw))


def _is_cmr(header_keys) -> bool:
    keys = set(header_keys)
    return "devicename" in keys and bool(
        keys & {"mos", "mlqk", "mlqkav", "mlqkmn"}
    )


class _Agg:
    __slots__ = ("total", "inbound", "outbound", "seconds", "last", "mos_sum",
                 "mos_count")

    def __init__(self):
        self.total = self.inbound = self.outbound = self.seconds = 0
        self.last: datetime | None = None
        self.mos_sum = 0.0
        self.mos_count = 0


def _fold_cdr(row: dict, agg: dict[str, _Agg]) -> None:
    orig = _pick(row, "origdevicename")
    dest = _pick(row, "destdevicename")
    duration = int(float(_pick(row, "duration") or 0))
    ts_raw = _pick(row, "datetimeorigination", "datetimeconnect")
    when = None
    if ts_raw:
        try:
            when = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            when = None

    for device, direction in ((orig, "out"), (dest, "in")):
        if not device or not str(device).startswith("SEP"):
            continue
        a = agg.setdefault(device, _Agg())
        a.total += 1
        a.seconds += duration
        if direction == "out":
            a.outbound += 1
        else:
            a.inbound += 1
        if when and (a.last is None or when > a.last):
            a.last = when


def _fold_cmr(row: dict, agg: dict[str, _Agg]) -> None:
    device = _pick(row, "devicename")
    if not device or not str(device).startswith("SEP"):
        return
    mos_raw = _pick(row, "mos", "mlqkav", "mlqk", "mlqkmn")
    try:
        mos = float(mos_raw)
    except (TypeError, ValueError):
        return
    if mos <= 0:
        return
    a = agg.setdefault(device, _Agg())
    a.mos_sum += mos
    a.mos_count += 1


def ingest_directory(session: Session, directory: str | Path) -> dict:
    """Fold every CDR/CMR file in a directory into CallStat rows.

    Additive: re-running with new files keeps accumulating. Returns a small
    summary for the CLI/logs.
    """
    directory = Path(directory)
    agg: dict[str, _Agg] = {}
    files = 0
    if directory.is_dir():
        for path in sorted(directory.glob("*")):
            if not path.is_file() or path.suffix.lower() not in {".csv", ".txt", ""}:
                continue
            sample = list(_first_header(path))
            if not sample:
                continue
            files += 1
            is_cmr = _is_cmr(sample)
            for row in _rows(path):
                (_fold_cmr if is_cmr else _fold_cdr)(row, agg)

    existing = {
        c.device_name: c for c in session.scalars(select(CallStat)).all()
    }
    for device, a in agg.items():
        stat = existing.get(device)
        if stat is None:
            stat = CallStat(
                device_name=device,
                total_calls=0, inbound_calls=0, outbound_calls=0,
                total_seconds=0, mos_sum=0.0, mos_count=0,
            )
            session.add(stat)
        stat.total_calls += a.total
        stat.inbound_calls += a.inbound
        stat.outbound_calls += a.outbound
        stat.total_seconds += a.seconds
        stat.mos_sum += a.mos_sum
        stat.mos_count += a.mos_count
        if a.last and (stat.last_call_at is None or a.last > stat.last_call_at):
            stat.last_call_at = a.last
        stat.updated_at = datetime.now(timezone.utc)

    return {"files": files, "devices": len(agg)}


def _first_header(path: Path):
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        try:
            return [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return []
