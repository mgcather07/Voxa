"""Run one collection and exit — the entry point for scheduled (cron) sync.

    python scripts/collect.py

Runs synchronously (so a scheduler sees it finish) and exits non-zero if the
collection failed, so cron mail / systemd status reflects the real outcome.
Voxa deliberately has no in-app scheduler: the OS (cron or a systemd timer) is
the scheduler, which keeps the dependency list short and the timing visible to
whoever operates the box. See docs/DEPLOY.md and deploy/voxa-collect.timer.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, session_scope  # noqa: E402
from app.models import SyncRun  # noqa: E402
from app.sync import run_sync  # noqa: E402


def main() -> int:
    init_db()
    run_id = run_sync()
    with session_scope() as session:
        run = session.get(SyncRun, run_id)
        status = run.status if run else "unknown"
        error = run.error if run else None
        created = run.created if run else 0
        updated = run.updated if run else 0
    print(f"Collection {run_id}: {status} ({created} new, {updated} updated)")
    if status != "success":
        print(error or "collection failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
