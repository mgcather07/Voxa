"""Collection run: AXL + RisPort + phone web -> one row per phone."""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import delete, select

from .catalog import get_catalog
from .config import Settings, get_settings
from . import settings_store, webhooks
from .cucm import AxlClient, RisPortClient, fetch_many
from .db import session_scope
from .history import latest_diff, record_snapshots
from .models import ClusterNode, Phone, SyncRun

log = logging.getLogger(__name__)

# Cluster topology: the CUCM nodes and their role. `EnterpriseWideData` is the
# pseudo-node, excluded. tkprocessnoderole -> Voice/Video vs IM&P.
_NODES_SQL = (
    "SELECT p.name AS name, p.description AS description, p.nodeid AS nodeid, "
    "r.name AS role FROM processnode p "
    "LEFT JOIN typeprocessnoderole r ON r.enum = p.tkprocessnoderole "
    "WHERE p.name != 'EnterpriseWideData' ORDER BY p.nodeid"
)


def derive_site(device_pool: str | None, pattern: str) -> str | None:
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


def _set_stage(run_id: int, stage: str, **counts: int) -> None:
    with session_scope() as session:
        run = session.get(SyncRun, run_id)
        if run:
            run.stage = stage
            for key, value in counts.items():
                setattr(run, key, value)


def run_sync(settings: Settings | None = None) -> int:
    """Execute a full collection. Returns the SyncRun id."""
    settings = settings or get_settings()
    catalog = get_catalog()

    with session_scope() as session:
        run = SyncRun(stage="starting")
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        conns = settings_store.clusters()
        cfg = settings_store.load()

        # --- 1-3. Collect from every enabled cluster ----------------------
        axl_phones: dict[str, tuple] = {}   # name -> (AxlPhone, cluster_name)
        ris_devices: dict[str, tuple] = {}  # name -> (RisDevice, cluster_name)
        web_info: dict = {}
        collected_nodes: list[dict] = []    # cluster topology from processnode
        for conn in conns:
            _set_stage(run_id, f"querying AXL ({conn.name})")
            axl = AxlClient(
                conn.host, conn.user, conn.password,
                version=conn.axl_version, verify=conn.verify_tls,
            )
            for p in axl.iter_phones():
                if p.name:
                    axl_phones[p.name] = (p, conn.name)
            _set_stage(run_id, f"querying RisPort ({conn.name})",
                       axl_count=len(axl_phones))

            ris = RisPortClient(conn.host, conn.user, conn.password,
                                verify=conn.verify_tls)
            devices = ris.fetch_all()
            for dname, dev in devices.items():
                ris_devices[dname] = (dev, conn.name)
            _set_stage(run_id, f"querying phones ({conn.name})",
                       ris_count=len(ris_devices))

            # Cluster topology + per-node registered counts (read-only, AXL).
            try:
                reg_by_node = Counter(
                    dev.cm_node for dev in devices.values()
                    if dev.cm_node and (dev.status or "").lower() == "registered"
                )
                for nr in axl.execute_sql(_NODES_SQL):
                    nm = nr.get("name")
                    if not nm:
                        continue
                    nid = str(nr.get("nodeid") or "")
                    collected_nodes.append({
                        "cluster": conn.name,
                        "name": nm,
                        "description": nr.get("description"),
                        "role": nr.get("role"),
                        "node_id": int(nid) if nid.isdigit() else None,
                        "registered_devices": reg_by_node.get(nm, 0),
                    })
            except Exception as exc:  # noqa: BLE001 - topology is best-effort
                log.warning("Cluster node collection failed (%s): %s",
                            conn.name, exc)

            if cfg.phone_web_enabled and conn.phone_web_enabled:
                ips = [
                    d.ip_address
                    for d in devices.values()
                    if d.ip_address and (d.status or "").lower() == "registered"
                ]
                web_info.update(fetch_many(
                    ips,
                    concurrency=cfg.phone_web_concurrency,
                    timeout=cfg.phone_web_timeout,
                ))
        reachable = sum(1 for i in web_info.values() if i.reachable)
        _set_stage(run_id, "writing database", web_count=reachable)

        # --- 4. Merge and upsert ------------------------------------------
        created = updated = 0
        now = datetime.now(timezone.utc)

        with session_scope() as session:
            existing = {
                p.device_name: p for p in session.scalars(select(Phone)).all()
            }

            all_names = set(axl_phones) | set(ris_devices)
            for name in all_names:
                axl_entry = axl_phones.get(name)
                ris_entry = ris_devices.get(name)
                axl_row = axl_entry[0] if axl_entry else None
                ris_row = ris_entry[0] if ris_entry else None
                cluster_name = (
                    axl_entry[1] if axl_entry
                    else ris_entry[1] if ris_entry else None
                )
                model_raw = (
                    (axl_row.model if axl_row else None)
                    or (ris_row.product if ris_row else None)
                )
                # Drop non-phones (Jabber/CTI/templates/3rd-party): skip new ones
                # and delete any previously collected.
                if catalog.is_excluded(model_raw):
                    prior = existing.get(name)
                    if prior is not None:
                        session.delete(prior)
                    continue
                info = catalog.lookup(model_raw)

                phone = existing.get(name)
                if phone is None:
                    phone = Phone(device_name=name, first_seen=now)
                    session.add(phone)
                    created += 1
                else:
                    updated += 1

                if axl_row:
                    phone.pkid = axl_row.pkid
                    phone.description = axl_row.description
                    phone.model_raw = axl_row.model
                    phone.protocol = axl_row.protocol
                    phone.device_pool = axl_row.device_pool
                    phone.site = derive_site(
                        axl_row.device_pool, cfg.site_from_device_pool
                    )
                    phone.configured_load = axl_row.load_information
                    phone.directory_number = axl_row.directory_number
                elif model_raw and not phone.model_raw:
                    phone.model_raw = model_raw

                if ris_row:
                    phone.registration_status = ris_row.status
                    phone.status_reason = ris_row.status_reason
                    phone.ip_address = ris_row.ip_address
                    phone.active_load = ris_row.active_load
                    phone.cm_node = ris_row.cm_node
                    if not phone.directory_number and ris_row.dir_number:
                        phone.directory_number = ris_row.dir_number
                else:
                    phone.registration_status = "Unknown"
                    phone.status_reason = "not reported by RisPort"

                web = web_info.get(phone.ip_address or "")
                if web and web.reachable:
                    phone.web_reachable = True
                    phone.serial_number = web.serial_number or phone.serial_number
                    phone.hardware_revision = (
                        web.hardware_revision or phone.hardware_revision
                    )
                    phone.switch_name = web.switch_name or phone.switch_name
                    phone.switch_port = web.switch_port or phone.switch_port
                    phone.switch_ip = web.switch_ip or phone.switch_ip
                    phone.vlan_id = web.vlan_id or phone.vlan_id
                elif web is not None:
                    phone.web_reachable = False

                if cluster_name:
                    phone.cluster = cluster_name
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

        # --- 4b. Persist cluster topology (replace per collected cluster) --
        if collected_nodes:
            with session_scope() as session:
                for cname in {n["cluster"] for n in collected_nodes}:
                    session.execute(
                        delete(ClusterNode).where(ClusterNode.cluster == cname)
                    )
                for n in collected_nodes:
                    session.add(ClusterNode(**n))

        # --- 5. Snapshot for change history ------------------------------
        with session_scope() as session:
            record_snapshots(session, run_id, datetime.now(timezone.utc))

        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            if run:
                run.status = "success"
                run.stage = "done"
                run.created = created
                run.updated = updated
                run.finished_at = datetime.now(timezone.utc)

        log.info("Sync %s complete: %s created, %s updated", run_id, created, updated)

        # Opt-in webhooks (no-op unless WEBHOOKS_ENABLED and a hook is enabled).
        with session_scope() as session:
            diff = latest_diff(session)
        if diff is not None:
            webhooks.fire("phones.changed", {
                "appeared": len(diff.appeared), "dropped": len(diff.dropped),
                "moved": len(diff.moved), "reg_changed": len(diff.reg_changed),
                "firmware_changed": len(diff.firmware_changed),
            })
        webhooks.fire("sync.completed", {
            "run_id": run_id, "created": created, "updated": updated,
        })

    except Exception as exc:  # noqa: BLE001 - surface the reason in the UI
        log.exception("Sync %s failed", run_id)
        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            if run:
                run.status = "failed"
                run.stage = "failed"
                run.error = f"{type(exc).__name__}: {exc}"
                run.finished_at = datetime.now(timezone.utc)

    return run_id
