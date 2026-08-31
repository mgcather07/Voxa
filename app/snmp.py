"""Poll access switches for real PoE numbers (read-only SNMP).

The PoE budget page estimates reserved power from IEEE class ceilings. That is
the right number for *planning* (it's what a switch reserves per port), but once
switches are reachable, SNMP gives the *actual* draw and the switch's total PoE
budget — so you can show real headroom next to the estimate.

Uses POWER-ETHERNET-MIB, summed across PSEs per switch:
  pethMainPseConsumptionPower  1.3.6.1.2.1.105.1.3.1.1.4   (watts drawn now)
  pethMainPsePower             1.3.6.1.2.1.105.1.3.1.1.2   (nominal budget, W)

Switch IPs are the CDP neighbours Voxa already discovered (Phone.switch_ip).
pysnmp is optional (requirements-snmp.txt); the base image ships without it.
Nothing here writes to a switch — every SNMP call is a GET/walk.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .config import Settings
from .models import Phone, SwitchPoll

log = logging.getLogger(__name__)

_OID_USED = "1.3.6.1.2.1.105.1.3.1.1.4"   # pethMainPseConsumptionPower
_OID_AVAIL = "1.3.6.1.2.1.105.1.3.1.1.2"  # pethMainPsePower


def poll_switch(host: str, settings: Settings) -> tuple[float | None, float | None]:
    """Return (available_watts, used_watts) for one switch, or (None, None).

    Raises NotImplementedError if pysnmp is not installed.
    """
    try:
        from pysnmp.hlapi import (  # type: ignore
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            nextCmd,
        )
    except ImportError as exc:  # pragma: no cover - env dependent
        raise NotImplementedError(
            "SNMP polling needs pysnmp: pip install -r requirements-snmp.txt"
        ) from exc

    def _walk(oid: str) -> float:
        total = 0.0
        for err_ind, err_stat, _idx, binds in nextCmd(
            SnmpEngine(),
            CommunityData(settings.snmp_community, mpModel=1),  # v2c
            UdpTransportTarget((host, 161), timeout=settings.snmp_timeout, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if err_ind or err_stat:
                break
            for _name, val in binds:
                try:
                    total += float(val)
                except (TypeError, ValueError):
                    pass
        return total

    try:
        avail = _walk(_OID_AVAIL)
        used = _walk(_OID_USED)
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("SNMP poll of %s failed: %s", host, exc)
        return None, None
    return (avail or None), (used or None)


def _switch_targets(session: Session) -> dict[str, str]:
    """{switch_name: switch_ip} for switches we have a CDP-discovered IP for."""
    rows = session.execute(
        select(Phone.switch_name, Phone.switch_ip)
        .where(Phone.switch_name.is_not(None), Phone.switch_ip.is_not(None))
        .distinct()
    ).all()
    targets: dict[str, str] = {}
    for name, ip in rows:
        targets.setdefault(name, ip)
    return targets


def poll_all(session: Session, settings=None) -> dict:
    """Poll every discovered switch and upsert SwitchPoll rows."""
    settings = settings or settings_store.load(session)
    targets = _switch_targets(session)
    existing = {p.switch_name: p for p in session.scalars(select(SwitchPoll)).all()}
    now = datetime.now(timezone.utc)
    polled = 0
    for name, ip in targets.items():
        avail, used = poll_switch(ip, settings)
        if avail is None and used is None:
            continue
        row = existing.get(name)
        if row is None:
            row = SwitchPoll(switch_name=name)
            session.add(row)
            existing[name] = row
        row.available_watts = avail
        row.used_watts = used
        row.polled_at = now
        polled += 1
    return {"targets": len(targets), "polled": polled}
