"""Import or augment the inventory from a CSV file.

    python scripts/import_csv.py devices.csv

Upserts phones by device_name and derives catalog fields from the model. Writes
only to Voxa's database. See app/importer.py for the recognised columns.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, session_scope  # noqa: E402
from app.importer import import_csv_text  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_csv.py <file.csv>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"No such file: {path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    init_db()
    with session_scope() as session:
        summary = import_csv_text(session, text)
    print(
        f"Imported {path.name}: {summary['created']} added, "
        f"{summary['updated']} updated, {summary['skipped']} skipped."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
