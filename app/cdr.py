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
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CallQuality, CallRecord, CallStat

log = logging.getLogger(__name__)


def _epoch(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _cdr_record(row: dict) -> CallRecord | None:
    mgr = _int(_pick(row, "globalcallid_callmanagerid", "globalcallidcallmanagerid"))
    cid = _int(_pick(row, "globalcallid_callid", "globalcallidcallid"))
    if mgr is None or cid is None:
        return None
    return CallRecord(
        call_mgr_id=mgr,
        call_id=cid,
        orig_leg_id=_int(_pick(row, "origlegcallidentifier")),
        dest_leg_id=_int(_pick(row, "destlegcallidentifier")),
        orig_device=_pick(row, "origdevicename"),
        dest_device=_pick(row, "destdevicename"),
        calling_number=_pick(row, "callingpartynumber"),
        original_called=_pick(row, "originalcalledpartynumber"),
        final_called=_pick(row, "finalcalledpartynumber", "originalcalledpartynumber"),
        orig_ip=_pick(row, "origipv4v6addr", "origipaddr"),
        dest_ip=_pick(row, "destipv4v6addr", "destipaddr"),
        orig_time=_epoch(_pick(row, "datetimeorigination")),
        connect_time=_epoch(_pick(row, "datetimeconnect")),
        disconnect_time=_epoch(_pick(row, "datetimedisconnect")),
        duration=_int(_pick(row, "duration")) or 0,
        orig_cause=_int(_pick(row, "origcause_value", "origcausevalue")),
        dest_cause=_int(_pick(row, "destcause_value", "destcausevalue")),
    )


def _cmr_quality(row: dict) -> CallQuality | None:
    leg = _int(_pick(row, "callidentifier"))
    if leg is None:
        return None
    vq = _vq_metrics(row)
    codec = vq.get("vorxcodec") or vq.get("votxcodec") or None
    return CallQuality(
        leg_id=leg,
        device=_pick(row, "devicename"),
        directory_number=_pick(row, "directorynum"),
        mos=_mos_from_row(row),
        jitter_ms=_float(_pick(row, "jitter")),
        latency_ms=_float(_pick(row, "latency")),
        packets_lost=_int(_pick(row, "numberpacketslost")),
        packets_sent=_int(_pick(row, "numberpacketssent")),
        codec=(codec[:48] if codec else None),
        concealed_secs=_int(vq.get("cs")),
        severely_concealed_secs=_int(vq.get("scs")),
    )


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick(row: dict, *names: str):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def _vq_metrics(row: dict) -> dict:
    """Parse CUCM's ``varVQMetrics`` blob ('K1=v1;K2=v2;…') to a lowercased
    dict. This is where modern CMR keeps the K-factor voice-quality metrics —
    MOS (MLQKav), codec (VoRxCodec), concealed seconds (CS/SCS) and more."""
    raw = _pick(row, "varvqmetrics")
    out: dict[str, str] = {}
    if raw:
        for part in str(raw).split(";"):
            key, sep, val = part.partition("=")
            if sep:
                out[key.strip().lower()] = val.strip()
    return out


def _mos_from_row(row: dict) -> float | None:
    """The call's MOS. Prefers a top-level column (older CUCM), then MLQKav
    (average MOS-LQK) from varVQMetrics. Returns None when no MOS was measured
    for the leg — never 0."""
    candidates = [_pick(row, "mos", "mlqkav", "mlqk", "mlqkmn")]
    vq = _vq_metrics(row)
    candidates += [vq.get("mlqkav"), vq.get("mlqk"), vq.get("mlqkmn")]
    for raw in candidates:
        if raw in (None, ""):
            continue
        try:
            mos = float(raw)
        except (TypeError, ValueError):
            continue
        if mos > 0:
            return mos
    return None


# A CUCM CSV type-row cell: an all-caps SQL type, optionally sized, e.g.
# INTEGER, VARCHAR(50), UNIQUEIDENTIFIER, DOUBLE PRECISION. Real data cells
# (numbers, IPs, SEP… device names, timestamps) never match this shape, so a
# row where *every* populated cell matches is CUCM's type row — whatever type
# names a given cluster/version emits. Matching by shape avoids hardcoding a
# list that misses one (as UNIQUEIDENTIFIER was).
_TYPE_CELL = re.compile(r"^[A-Z][A-Z_ ]*(\([0-9, ]+\))?$")


def _is_type_row(cells) -> bool:
    seen = False
    for c in cells:
        c = c.strip().strip('"').strip()
        if not c:
            continue
        seen = True
        if not _TYPE_CELL.match(c):
            return False
    return seen


def _rows(path: Path):
    """Yield dict rows from a CUCM CSV, keys lower-cased, type row(s) skipped."""
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return
        for raw in reader:
            if not raw:
                continue
            if _is_type_row(raw):  # CUCM's type row, wherever it appears
                continue
            yield dict(zip(header, raw))


def _is_cmr(header_keys) -> bool:
    """A CMR (quality) file, not a CDR. CMR carries per-stream stats keyed by a
    single ``deviceName``; modern CUCM puts MOS in ``varVQMetrics`` rather than
    a top-level column, so detect by any of those signals. CDR instead has
    ``origDeviceName``/``destDeviceName`` and no packet counts."""
    keys = set(header_keys)
    if "varvqmetrics" in keys:
        return True
    return "devicename" in keys and (
        "numberpacketssent" in keys or bool(keys & {"mos", "mlqk", "mlqkav", "mlqkmn"})
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
    duration = _int(_pick(row, "duration")) or 0
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
    mos = _mos_from_row(row)
    if not mos:
        return
    a = agg.setdefault(device, _Agg())
    a.mos_sum += mos
    a.mos_count += 1


# Files still mid-transfer or not call data — never ingest these.
_SKIP_SUFFIXES = (".tmp", ".part", ".filepart", ".writing")
_ARCHIVE_DIR = "processed"


def ingest_directory(session: Session, directory: str | Path) -> dict:
    """Fold every new CDR/CMR file in a directory into CallStat rows.

    Ingest is additive, so each file must be counted exactly once. This reads
    only the top level of ``directory`` (the SFTP landing spot) and returns the
    paths it consumed in ``processed`` — the caller archives them with
    :func:`archive_files` *after* the DB commit, so a file is moved out of the
    landing spot only once its data is durably saved. On the next run those
    files are gone, so nothing is double-counted.

    Files in the ``processed/`` archive subdirectory, partial transfers
    (``.tmp``/``.part``/…), and empty files are skipped.
    """
    directory = Path(directory)
    agg: dict[str, _Agg] = {}
    consumed: list[Path] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*")):
            if path.is_dir():
                continue  # skip processed/ and any other subdirectory
            if not path.is_file() or path.suffix.lower() not in {".csv", ".txt", ""}:
                continue
            if path.name.endswith(_SKIP_SUFFIXES):
                continue
            try:
                if path.stat().st_size == 0:
                    continue  # empty / still being written
            except OSError:
                continue
            sample = list(_first_header(path))
            if not sample:
                continue
            is_cmr = _is_cmr(sample)
            for row in _rows(path):
                if is_cmr:
                    _fold_cmr(row, agg)
                    quality = _cmr_quality(row)
                    if quality is not None:
                        session.add(quality)
                else:
                    _fold_cdr(row, agg)
                    record = _cdr_record(row)
                    if record is not None:
                        session.add(record)
            consumed.append(path)

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

    return {"files": len(consumed), "devices": len(agg),
            "processed": [str(p) for p in consumed]}


def archive_files(directory: str | Path, paths: list[str]) -> int:
    """Move consumed files into ``<directory>/processed/`` so a later run never
    re-ingests them. Call only after the ingest transaction has committed. A
    move that fails is logged, not fatal — the file simply stays and would be
    re-read next run (a rare double-count beats losing the file)."""
    if not paths:
        return 0
    archive = Path(directory) / _ARCHIVE_DIR
    try:
        archive.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("CDR: could not create archive dir %s: %s", archive, exc)
        return 0
    moved = 0
    for p in paths:
        src = Path(p)
        try:
            src.rename(archive / src.name)
            moved += 1
        except OSError as exc:
            log.warning("CDR: could not archive %s: %s", src.name, exc)
    return moved


def prune_processed(directory: str | Path, days: int) -> int:
    """Delete archived files older than ``days`` from ``<directory>/processed/``
    so the local archive stays bounded. ``days`` <= 0 keeps everything. Returns
    how many were removed."""
    if not days or days <= 0:
        return 0
    archive = Path(directory) / _ARCHIVE_DIR
    if not archive.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for p in archive.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as exc:  # noqa: PERF203
            log.warning("CDR: could not prune %s: %s", p.name, exc)
    return removed


def _first_header(path: Path):
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        try:
            return [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return []
