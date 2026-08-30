"""Prove the CUCM service account works before you debug the whole app.

    python scripts/test_cucm.py

Checks AXL, RisPort, and (if a registered phone is found) the phone web
scrape, printing exactly which one failed and why.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.cucm import AxlClient, CucmError, RisPortClient, fetch_one  # noqa: E402


def main() -> int:
    s = get_settings()
    print(f"CUCM host : {s.cucm_host}")
    print(f"User      : {s.cucm_user}")
    print(f"AXL ver   : {s.cucm_axl_version}")
    print("-" * 60)

    # --- AXL ---------------------------------------------------------------
    axl = AxlClient(
        s.cucm_host,
        s.cucm_user,
        s.cucm_password,
        version=s.cucm_axl_version,
        verify=s.cucm_verify_tls,
    )
    try:
        version = axl.test_connection()
        print(f"[ok]   AXL reachable. Cluster component version: {version}")
    except CucmError as exc:
        print(f"[FAIL] AXL: {exc}")
        print("       Check 'Standard AXL API Access' on the Application User,")
        print("       and that you are pointed at the PUBLISHER.")
        return 1

    try:
        phones = []
        for phone in axl.iter_phones(page_size=5):
            phones.append(phone)
            if len(phones) >= 5:
                break
        print(f"[ok]   AXL phone query returned {len(phones)} sample rows.")
        for p in phones[:3]:
            print(f"         {p.name:20} {p.model or '?':22} {p.description or ''}")
    except CucmError as exc:
        print(f"[FAIL] AXL phone query: {exc}")
        return 1

    # --- RisPort -----------------------------------------------------------
    ris = RisPortClient(
        s.cucm_host, s.cucm_user, s.cucm_password, verify=s.cucm_verify_tls
    )
    try:
        devices = ris.fetch_all(max_pages=1)
        registered = [d for d in devices.values() if (d.status or "") == "Registered"]
        print(f"[ok]   RisPort returned {len(devices)} devices "
              f"({len(registered)} registered) on the first page.")
    except CucmError as exc:
        print(f"[FAIL] RisPort: {exc}")
        print("       Check 'Standard CCM Admin Users (Read Only)' and")
        print("       'Standard Serviceability (Read Only)' on the same user.")
        return 1

    # --- Phone web ---------------------------------------------------------
    if not registered:
        print("[skip] No registered phone to test the web scrape against.")
        return 0

    target = registered[0]
    print("-" * 60)
    print(f"Testing phone web scrape against {target.name} ({target.ip_address})")
    info = fetch_one(target.ip_address or "", timeout=s.phone_web_timeout)
    if info.reachable:
        print(f"[ok]   serial={info.serial_number} "
              f"switch={info.switch_name} port={info.switch_port}")
    else:
        print(f"[warn] Phone did not respond: {info.error}")
        print("       Serial numbers and switch ports will be blank.")
        print("       Enable 'Web Access' on the phone or Common Phone Profile,")
        print("       and confirm this machine can reach the phone subnets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
