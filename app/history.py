"""Change history and fleet trend over collection runs.

Every collection writes a `PhoneSnapshot` per phone. Diffing consecutive runs
answers the operator question a plain inventory can't: *what changed?* — which
phones appeared, dropped off, moved to a different switch port, re-registered,
or took a firmware bump. This is what turns a one-time refresh tool into
something the team keeps using afterward.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Phone, PhoneSnapshot, SyncRun


def record_snapshots(session: Session, sync_run_id: int, captured_at) -> int:
    """Snapshot every current phone for the given run. Returns the count."""
    phones = session.scalars(select(Phone)).all()
    session.add_all(
        PhoneSnapshot(
            sync_run_id=sync_run_id,
            device_name=p.device_name,
            registration_status=p.registration_status,
            ip_address=p.ip_address,
            switch_name=p.switch_name,
            switch_port=p.switch_port,
            active_load=p.active_load,
            has_serial=p.serial_number is not None,
            captured_at=captured_at,
        )
        for p in phones
    )
    return len(phones)


def recent_runs(session: Session, limit: int = 30) -> list[SyncRun]:
    """Successful runs that actually captured snapshots, newest first."""
    with_snaps = select(func.distinct(PhoneSnapshot.sync_run_id)).subquery()
    return list(
        session.scalars(
            select(SyncRun)
            .where(SyncRun.id.in_(select(with_snaps)))
            .order_by(SyncRun.id.desc())
            .limit(limit)
        ).all()
    )


def _snapshot_map(session: Session, run_id: int) -> dict[str, PhoneSnapshot]:
    rows = session.scalars(
        select(PhoneSnapshot).where(PhoneSnapshot.sync_run_id == run_id)
    ).all()
    return {r.device_name: r for r in rows}


@dataclass
class RunDiff:
    current: SyncRun
    previous: SyncRun | None
    appeared: list[str]
    dropped: list[str]
    reg_changed: list[dict]
    moved: list[dict]
    firmware_changed: list[dict]

    @property
    def total_changes(self) -> int:
        return (
            len(self.appeared)
            + len(self.dropped)
            + len(self.reg_changed)
            + len(self.moved)
            + len(self.firmware_changed)
        )


def diff_runs(session: Session, current: SyncRun, previous: SyncRun | None) -> RunDiff:
    cur = _snapshot_map(session, current.id)
    prev = _snapshot_map(session, previous.id) if previous else {}

    appeared = sorted(set(cur) - set(prev))
    dropped = sorted(set(prev) - set(cur))
    reg_changed: list[dict] = []
    moved: list[dict] = []
    firmware_changed: list[dict] = []

    for name in sorted(set(cur) & set(prev)):
        c, p = cur[name], prev[name]
        if (c.registration_status or "") != (p.registration_status or ""):
            reg_changed.append(
                {"device": name, "was": p.registration_status or "—",
                 "now": c.registration_status or "—"}
            )
        c_port = f"{c.switch_name or ''} · {c.switch_port or ''}".strip(" ·")
        p_port = f"{p.switch_name or ''} · {p.switch_port or ''}".strip(" ·")
        if c_port != p_port:
            moved.append(
                {"device": name, "was": p_port or "not reachable",
                 "now": c_port or "not reachable"}
            )
        if (c.active_load or "") != (p.active_load or ""):
            firmware_changed.append(
                {"device": name, "was": p.active_load or "—",
                 "now": c.active_load or "—"}
            )

    return RunDiff(current, previous, appeared, dropped, reg_changed, moved,
                   firmware_changed)


def latest_diff(session: Session) -> RunDiff | None:
    runs = recent_runs(session, limit=2)
    if not runs:
        return None
    current = runs[0]
    previous = runs[1] if len(runs) > 1 else None
    return diff_runs(session, current, previous)


def diff_for_run(session: Session, run_id: int) -> RunDiff | None:
    runs = recent_runs(session, limit=60)
    for i, run in enumerate(runs):
        if run.id == run_id:
            previous = runs[i + 1] if i + 1 < len(runs) else None
            return diff_runs(session, run, previous)
    return None


def fleet_trend(session: Session, limit: int = 12) -> list[dict]:
    """Per run: fleet size, registered %, serial %, port % — oldest to newest."""
    runs = list(reversed(recent_runs(session, limit=limit)))
    out = []
    for run in runs:
        total = session.scalar(
            select(func.count()).select_from(PhoneSnapshot)
            .where(PhoneSnapshot.sync_run_id == run.id)
        ) or 0
        registered = session.scalar(
            select(func.count()).select_from(PhoneSnapshot).where(
                PhoneSnapshot.sync_run_id == run.id,
                PhoneSnapshot.registration_status == "Registered",
            )
        ) or 0
        with_switch = session.scalar(
            select(func.count()).select_from(PhoneSnapshot).where(
                PhoneSnapshot.sync_run_id == run.id,
                PhoneSnapshot.switch_name.is_not(None),
            )
        ) or 0
        with_serial = session.scalar(
            select(func.count()).select_from(PhoneSnapshot).where(
                PhoneSnapshot.sync_run_id == run.id,
                PhoneSnapshot.has_serial.is_(True),
            )
        ) or 0
        out.append(
            {
                "run": run,
                "total": total,
                "registered_pct": round(100 * registered / total) if total else 0,
                "serial_pct": round(100 * with_serial / total) if total else 0,
                "port_pct": round(100 * with_switch / total) if total else 0,
            }
        )
    return out


def device_timeline(session: Session, device_name: str) -> list[dict]:
    """Change-points for one device across runs, most recent first."""
    snaps = session.scalars(
        select(PhoneSnapshot)
        .where(PhoneSnapshot.device_name == device_name)
        .order_by(PhoneSnapshot.sync_run_id.asc())
    ).all()
    events: list[dict] = []
    prev: PhoneSnapshot | None = None
    for s in snaps:
        changes = []
        if prev is None:
            changes.append("first seen")
        else:
            if (s.registration_status or "") != (prev.registration_status or ""):
                changes.append(
                    f"registration {prev.registration_status or '—'} → "
                    f"{s.registration_status or '—'}"
                )
            c_port = f"{s.switch_name or ''} · {s.switch_port or ''}".strip(" ·")
            p_port = f"{prev.switch_name or ''} · {prev.switch_port or ''}".strip(" ·")
            if c_port != p_port:
                changes.append(f"moved to {c_port or 'not reachable'}")
            if (s.active_load or "") != (prev.active_load or ""):
                changes.append(
                    f"firmware {prev.active_load or '—'} → {s.active_load or '—'}"
                )
        if changes:
            events.append({"captured_at": s.captured_at, "changes": changes})
        prev = s
    events.reverse()
    return events
