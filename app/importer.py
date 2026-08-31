"""Import or augment the inventory from a CSV.

Call Telemetry's "CSV lists" discovery: when the phone web scrape is blocked or
a phone is unreachable, an operator can still load device facts (serial, switch,
port…) from a spreadsheet. Upserts by device name and derives catalog fields
(lifecycle, PoE, replacement) from the model, exactly like a collection would.
This writes only to Voxa's own database — never to CUCM.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import get_catalog
from .config import get_settings
from .models import Phone

# CSV header (lower-cased) -> Phone attribute. Unknown columns are ignored.
_FIELD_MAP = {
    "device_name": "device_name",
    "device": "device_name",
    "name": "device_name",
    "description": "description",
    "directory_number": "directory_number",
    "dn": "directory_number",
    "model": "model_raw",
    "model_raw": "model_raw",
    "device_pool": "device_pool",
    "site": "site",
    "ip_address": "ip_address",
    "ip": "ip_address",
    "registration_status": "registration_status",
    "serial_number": "serial_number",
    "serial": "serial_number",
    "hardware_revision": "hardware_revision",
    "switch_name": "switch_name",
    "switch": "switch_name",
    "switch_port": "switch_port",
    "port": "switch_port",
    "vlan_id": "vlan_id",
    "vlan": "vlan_id",
}


def _derive_site(device_pool: str | None) -> str | None:
    pattern = get_settings().site_from_device_pool
    if not device_pool:
        return None
    try:
        match = re.search(pattern, device_pool)
    except re.error:
        return device_pool
    if match and match.groupdict().get("site"):
        return match.group("site")
    if match and match.groups():
        return match.group(1)
    return device_pool


def import_rows(session: Session, rows: list[dict]) -> dict:
    """Upsert phones from a list of raw CSV dict rows. Returns a summary."""
    catalog = get_catalog()
    now = datetime.now(timezone.utc)
    existing = {p.device_name: p for p in session.scalars(select(Phone)).all()}

    created = updated = skipped = 0
    for raw in rows:
        norm = {}
        for key, value in raw.items():
            if key is None:
                continue
            attr = _FIELD_MAP.get(key.strip().lower())
            if attr:
                norm[attr] = (value or "").strip() or None

        name = norm.get("device_name")
        if not name:
            skipped += 1
            continue

        phone = existing.get(name)
        if phone is None:
            phone = Phone(device_name=name, first_seen=now)
            session.add(phone)
            existing[name] = phone
            created += 1
        else:
            updated += 1

        for attr, value in norm.items():
            if attr == "device_name":
                continue
            if value is not None:
                setattr(phone, attr, value)

        if norm.get("device_pool") and not norm.get("site"):
            phone.site = _derive_site(norm["device_pool"])

        info = catalog.lookup(phone.model_raw)
        phone.model_key = info.key
        phone.family = info.family
        phone.generation = info.generation
        phone.lifecycle = info.lifecycle
        phone.poe_class = info.poe_class
        phone.poe_watts = info.poe_watts
        phone.replacement_key = info.replacement_key
        phone.replacement_name = info.replacement_name
        phone.replacement_poe_watts = info.replacement_poe_watts
        phone.last_seen = now

    return {"created": created, "updated": updated, "skipped": skipped}


def import_csv_text(session: Session, text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    return import_rows(session, list(reader))
