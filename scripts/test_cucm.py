"""Prove the CUCM service account works before you debug the whole app.

    python scripts/test_cucm.py

Checks AXL, RisPort, and (if a registered phone is found) the phone web
scrape, printing exactly which one failed and why.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import settings_store  # noqa: E402
from app.cucm_probe import probe  # noqa: E402


def main() -> int:
    conns = settings_store.clusters()
    print(f"Checking {len(conns)} configured cluster(s).")
    failed = 0
    for conn in conns:
        print("-" * 60)
        print(f"Cluster   : {conn.name}")
        print(f"CUCM host : {conn.host}")
        print(f"User      : {conn.user}")
        print(f"AXL ver   : {conn.axl_version}")
        for r in probe(conn):
            tag = "ok " if r["ok"] else "FAIL"
            print(f"[{tag}] {r['check']}: {r['detail']}")
            if not r["ok"]:
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
