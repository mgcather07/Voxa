"""Write every report template to files — for scheduled report delivery.

    python scripts/report.py /path/to/out    # CSV + XLSX per report

Pair with cron (like scripts/collect.py) and let the OS ship the files
(scp/sftp/rsync). Keeping the transfer in the OS avoids an SFTP-client
dependency in the app and keeps the timing visible to whoever runs the box.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import exports, report_templates  # noqa: E402
from app.db import session_scope  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "reports"
    out.mkdir(parents=True, exist_ok=True)

    with session_scope() as session:
        for key, title, _ in report_templates.REPORT_META:
            report = report_templates.build(session, key)
            if not report:
                continue
            csv_path = out / f"voxa-{key}.csv"
            with csv_path.open("w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(report["columns"])
                writer.writerows(report["rows"])
            xlsx_path = out / f"voxa-{key}.xlsx"
            xlsx_path.write_bytes(
                exports.write_xlsx(report["columns"], report["rows"], report["title"])
            )
            print(f"{title}: {len(report['rows'])} rows -> {csv_path.name}, {xlsx_path.name}")
    print(f"Wrote reports to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
