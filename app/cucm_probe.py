"""Probe a CUCM cluster connection (the Settings 'Test connection' button).

Runs the same checks as scripts/test_cucm.py — AXL, RisPort, and a phone web
scrape — but for one configured cluster, returning structured results. Read-only:
every call is a GET/query, nothing is written to CUCM.
"""

from __future__ import annotations

from .cucm import AxlClient, CucmError, RisPortClient, fetch_one


# Short timeout so the "Test connection" button fails fast against an
# unreachable host instead of blocking on the 120s client default.
_PROBE_TIMEOUT = 8.0


def probe(conn) -> list[dict]:
    """Return [{check, ok, detail}] for a ClusterConn-like object."""
    results: list[dict] = []

    try:
        axl = AxlClient(
            conn.host, conn.user, conn.password,
            version=conn.axl_version, verify=conn.verify_tls,
            timeout=_PROBE_TIMEOUT,
        )
        version = axl.test_connection()
        results.append({"check": "AXL", "ok": True,
                        "detail": f"reachable (component version {version})"})
    except CucmError as exc:
        results.append({"check": "AXL", "ok": False, "detail": str(exc)})
        return results  # nothing else will work without AXL
    except Exception as exc:  # noqa: BLE001 - network/DNS/TLS surface here
        results.append({"check": "AXL", "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}"})
        return results

    registered = []
    try:
        ris = RisPortClient(conn.host, conn.user, conn.password,
                            verify=conn.verify_tls, timeout=_PROBE_TIMEOUT)
        devices = ris.fetch_all(max_pages=1)
        registered = [d for d in devices.values() if (d.status or "") == "Registered"]
        results.append({"check": "RisPort", "ok": True,
                        "detail": f"{len(devices)} devices, {len(registered)} registered (page 1)"})
    except CucmError as exc:
        results.append({"check": "RisPort", "ok": False, "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001
        results.append({"check": "RisPort", "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}"})

    if conn.phone_web_enabled and registered:
        target = registered[0]
        info = fetch_one(target.ip_address or "", timeout=4)
        if info.reachable:
            results.append({"check": "Phone web", "ok": True,
                            "detail": f"{target.name}: serial={info.serial_number} switch={info.switch_name}"})
        else:
            results.append({"check": "Phone web", "ok": False,
                            "detail": f"{target.name} did not respond: {info.error}"})
    return results
