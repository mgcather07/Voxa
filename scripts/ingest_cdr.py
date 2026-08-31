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

from app import cdr  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402


def main() -> int:
    init_db()
    directory = sys.argv[1] if len(sys.argv) > 1 else get_settings().cdr_dir
    with session_scope() as session:
        result = cdr.ingest_directory(session, directory)
    print(
        f"Ingested {result['files']} file(s) from {directory}; "
        f"updated {result['devices']} device(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
