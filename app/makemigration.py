"""Generate a new Alembic revision from model changes.

    python -m app.makemigration "add foo to bar"

Autogenerates against the CURRENT database (diffs app.models vs. the live
schema), writes a revision under app/migrations/versions, then you review it and
commit. Startup (`init_db`) applies it. Run migrations manually with:

    python -m app.migrate            # upgrade to head
    python -m app.migrate <rev>      # upgrade/downgrade to a specific revision
"""

from __future__ import annotations

import sys

from alembic import command

from .db import alembic_config


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    message = " ".join(argv)
    command.revision(alembic_config(), message=message, autogenerate=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
