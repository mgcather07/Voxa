"""Fold CUCM CDR/CMR files into per-device call aggregates.

    python scripts/ingest_cdr.py [directory]

Defaults to the CDR_DIR setting. Pair with cron (like scripts/collect.py); the
OS/SFTP lands the files, this reads them. Additive — safe to run repeatedly as
new files arrive.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app import cdr, settings_store, webhooks  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import CallQuality  # noqa: E402


def main() -> int:
    init_db()
    directory = sys.argv[1] if len(sys.argv) > 1 else settings_store.load().cdr_dir
    with session_scope() as session:
        result = cdr.ingest_directory(session, directory)
        poor = session.scalar(
            select(func.count()).select_from(CallQuality)
            .where(CallQuality.mos.is_not(None), CallQuality.mos < 3.6)
        ) or 0
    # Archive consumed files only after the transaction above has committed, so
    # a file is moved out of the landing directory only once its data is saved.
    archived = cdr.archive_files(directory, result.get("processed", []))
    print(
        f"Ingested {result['files']} file(s) from {directory}; "
        f"updated {result['devices']} device(s); archived {archived} to processed/."
    )
    # Opt-in webhook (no-op unless enabled).
    webhooks.fire("call.quality_alert", {"poor_quality_legs": int(poor)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
