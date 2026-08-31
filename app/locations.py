"""E911 location mapping.

Voxa already discovers each phone's switch, port and IP. That is the hard half
of a dispatchable-location record. This module maps that discovered data to
operator-defined `Location`s via `LocationRule`s (a switch-name prefix, or an
IP subnet), so a 911 export can say *where* a phone physically is. All of this
is Voxa-owned data — nothing here writes to CUCM.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Location, LocationRule, Phone

MATCH_TYPES = ("switch", "subnet")


@dataclass
class _Rule:
    location_id: int
    match_type: str
    pattern: str

    def matches(self, switch_name: str | None, ip_address: str | None) -> bool:
        if self.match_type == "switch":
            return bool(switch_name) and switch_name.lower().startswith(
                self.pattern.lower()
            )
        if self.match_type == "subnet":
            if not ip_address:
                return False
            if "/" in self.pattern:
                try:
                    net = ipaddress.ip_network(self.pattern, strict=False)
                    return ipaddress.ip_address(ip_address) in net
                except ValueError:
                    return False
            return ip_address.startswith(self.pattern)


def _load_rules(session: Session) -> list[_Rule]:
    rows = session.execute(
        select(
            LocationRule.location_id, LocationRule.match_type, LocationRule.pattern
        )
    ).all()
    rules = [_Rule(lid, mt, pat) for lid, mt, pat in rows]
    # Most specific (longest pattern) wins.
    rules.sort(key=lambda r: len(r.pattern), reverse=True)
    return rules


def resolve_one(
    session: Session, switch_name: str | None, ip_address: str | None
) -> int | None:
    for rule in _load_rules(session):
        if rule.matches(switch_name, ip_address):
            return rule.location_id
    return None


def locations_by_id(session: Session) -> dict[int, Location]:
    return {loc.id: loc for loc in session.scalars(select(Location)).all()}


def resolve_phone(session: Session, phone: Phone) -> Location | None:
    loc_id = resolve_one(session, phone.switch_name, phone.ip_address)
    return session.get(Location, loc_id) if loc_id else None


def resolve_all(session: Session) -> dict[int, Location | None]:
    """phone.id -> Location (or None). Rules loaded once."""
    rules = _load_rules(session)
    locs = locations_by_id(session)
    out: dict[int, Location | None] = {}
    for phone in session.scalars(select(Phone)).all():
        match = next(
            (
                r.location_id
                for r in rules
                if r.matches(phone.switch_name, phone.ip_address)
            ),
            None,
        )
        out[phone.id] = locs.get(match) if match else None
    return out


def coverage(session: Session) -> dict:
    """How many phones resolve to a location — the E911 completeness number."""
    total = session.scalar(select(func.count()).select_from(Phone)) or 0
    resolved = sum(1 for loc in resolve_all(session).values() if loc)
    return {
        "total": total,
        "resolved": resolved,
        "pct": round(100 * resolved / total) if total else 0,
    }


def location_list(session: Session) -> list[dict]:
    """Locations with their rules and how many phones each currently matches."""
    resolved = resolve_all(session)
    counts: dict[int, int] = {}
    for loc in resolved.values():
        if loc:
            counts[loc.id] = counts.get(loc.id, 0) + 1

    out = []
    for loc in session.scalars(select(Location).order_by(Location.name)).all():
        rules = session.scalars(
            select(LocationRule).where(LocationRule.location_id == loc.id)
        ).all()
        out.append(
            {
                "location": loc,
                "rules": rules,
                "phone_count": counts.get(loc.id, 0),
            }
        )
    return out
