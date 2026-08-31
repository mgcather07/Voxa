"""Probe a CUCM cluster connection (the Settings 'Test connection' button).

Runs the same checks as scripts/test_cucm.py — AXL, RisPort, a phone web scrape —
for one configured cluster, plus discovers the cluster nodes (publisher +
subscribers) so the UI can show them. Read-only: every call is a GET/query.

You connect to the **publisher** only. AXL runs there and RisPort returns
cluster-wide registration state, so a single connection covers the whole
cluster — the subscribers are discovered, not configured separately.
"""

from __future__ import annotations

from .cucm import AxlClient, CucmError, RisPortClient, fetch_one

# Short timeout so the "Test connection" button fails fast against an
# unreachable host instead of blocking on the 120s client default.
_PROBE_TIMEOUT = 8.0

# processnode holds the cluster members; the pseudo-node is excluded.
_NODES_SQL = (
    "SELECT name, description FROM processnode "
    "WHERE name != 'EnterpriseWideData' ORDER BY name"
)


def probe(conn) -> dict:
    """Return {ok, checks: [{check, ok, detail}], nodes: [str]} for a
    ClusterConn-like object."""
    checks: list[dict] = []
    nodes: list[str] = []

    axl = AxlClient(
        conn.host, conn.user, conn.password,
        version=conn.axl_version, verify=conn.verify_tls,
        timeout=_PROBE_TIMEOUT,
    )
    try:
        version = axl.test_connection()
        checks.append({"check": "AXL", "ok": True,
                       "detail": f"reachable (component version {version})"})
    except CucmError as exc:
        checks.append({"check": "AXL", "ok": False, "detail": str(exc)})
        return {"ok": False, "checks": checks, "nodes": nodes}
    except Exception as exc:  # noqa: BLE001 - DNS/TLS/socket surface here
        checks.append({"check": "AXL", "ok": False,
                       "detail": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "checks": checks, "nodes": nodes}

    # --- Cluster nodes (publisher + subscribers) --------------------------
    try:
        rows = axl.execute_sql(_NODES_SQL)
        for r in rows:
            name = r.get("name") or ""
            if not name:
                continue
            pub = name.lower() == conn.host.lower() or (r.get("description") or "")
            label = name + (" (publisher)" if name.lower() == conn.host.lower() else "")
            nodes.append(label)
        checks.append({"check": "Cluster nodes", "ok": True,
                       "detail": f"{len(nodes)} node(s): " + ", ".join(nodes)})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "Cluster nodes", "ok": False,
                       "detail": f"could not read processnode: {exc}"})

    # --- RisPort ----------------------------------------------------------
    registered = []
    try:
        ris = RisPortClient(conn.host, conn.user, conn.password,
                            verify=conn.verify_tls, timeout=_PROBE_TIMEOUT)
        devices = ris.fetch_all(max_pages=1)
        registered = [d for d in devices.values() if (d.status or "") == "Registered"]
        checks.append({"check": "RisPort", "ok": True,
                       "detail": f"{len(devices)} devices, {len(registered)} registered (page 1)"})
    except CucmError as exc:
        checks.append({"check": "RisPort", "ok": False, "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "RisPort", "ok": False,
                       "detail": f"{type(exc).__name__}: {exc}"})

    # --- Phone web --------------------------------------------------------
    if conn.phone_web_enabled and registered:
        target = registered[0]
        info = fetch_one(target.ip_address or "", timeout=4)
        if info.reachable:
            checks.append({"check": "Phone web", "ok": True,
                           "detail": f"{target.name}: serial={info.serial_number} switch={info.switch_name}"})
        else:
            checks.append({"check": "Phone web", "ok": False,
                           "detail": f"{target.name} did not respond: {info.error}"})

    overall = all(c["ok"] for c in checks if c["check"] in ("AXL", "RisPort"))
    return {"ok": overall, "checks": checks, "nodes": nodes}
