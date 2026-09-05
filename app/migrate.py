"""Apply database migrations by hand (startup does this automatically too).

    python -m app.migrate           # upgrade to the latest revision
    python -m app.migrate <rev>     # move to a specific revision (up or down)
    python -m app.migrate current   # print the DB's current revision
"""

from __future__ import annotations

import sys

from alembic import command

from .db import alembic_config


def main(argv: list[str]) -> int:
    target = argv[0] if argv else "head"
    cfg = alembic_config()
    if target == "current":
        command.current(cfg, verbose=True)
    else:
        command.upgrade(cfg, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
