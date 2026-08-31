"""Poll access switches via SNMP for real PoE draw / budget.

    python scripts/poll_switches.py

Read-only SNMP against the switches Voxa already discovered by CDP. Needs pysnmp
(requirements-snmp.txt) and SNMP_ENABLED / SNMP_COMMUNITY set. Pair with cron
like the other collectors.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import snmp  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.snmp_enabled:
        print("SNMP polling is disabled (set SNMP_ENABLED=true).", file=sys.stderr)
        return 1
    init_db()
    try:
        with session_scope() as session:
            result = snmp.poll_all(session, settings)
    except NotImplementedError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Polled {result['polled']} of {result['targets']} switch(es).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
