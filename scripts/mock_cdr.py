"""Generate sample CUCM CDR/CMR files for local testing of call ingest.

    python scripts/mock_cdr.py [directory] [--calls N]

Writes one CDR file and one CMR file in CUCM's CSV format (header row, a column
*type* row, then data rows) into `directory` (default: the CDR_DIR setting).
Uses REAL SEP device names + DNs from the collected inventory, so the Calls,
Analytics and Capacity pages — and Phone 360 — link to actual phones.

This never touches CUCM; it only fabricates the flat files CUCM would normally
push to an SFTP endpoint. Ingest them with:

    python scripts/ingest_cdr.py [directory]

Note: ingest is additive and has no per-file de-dup, so ingest each generated
batch exactly once (re-running over the same files double-counts).
"""

from __future__ import annotations

import csv
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import settings_store  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Phone  # noqa: E402

CDR_FIELDS = [
    "cdrRecordType", "globalCallID_callManagerId", "globalCallID_callId",
    "origLegCallIdentifier", "destLegCallIdentifier",
    "dateTimeOrigination", "dateTimeConnect", "dateTimeDisconnect",
    "callingPartyNumber", "originalCalledPartyNumber", "finalCalledPartyNumber",
    "origDeviceName", "destDeviceName", "origIpv4v6Addr", "destIpv4v6Addr",
    "origCause_value", "destCause_value", "duration",
]
CDR_TYPES = [
    "INTEGER", "INTEGER", "INTEGER", "INTEGER", "INTEGER", "INTEGER", "INTEGER",
    "INTEGER", "VARCHAR(50)", "VARCHAR(50)", "VARCHAR(50)", "VARCHAR(129)",
    "VARCHAR(129)", "VARCHAR(64)", "VARCHAR(64)", "INTEGER", "INTEGER", "INTEGER",
]

CMR_FIELDS = [
    "cdrRecordType", "globalCallID_callManagerId", "globalCallID_callId",
    "callIdentifier", "deviceName", "directoryNum",
    "numberPacketsSent", "numberPacketsReceived", "numberPacketsLost",
    "jitter", "latency", "mos",
]
CMR_TYPES = [
    "INTEGER", "INTEGER", "INTEGER", "INTEGER", "VARCHAR(129)", "VARCHAR(50)",
    "INTEGER", "INTEGER", "INTEGER", "INTEGER", "INTEGER", "FLOAT",
]

# Non-SEP destinations for external calls, so the Gateways/trunks and Capacity
# views have PSTN traffic to group.
TRUNKS = ["SIPTRUNK_PSTN_HQ", "MGCP_GW_PLANT1", "SIPTRUNK_CARRIER_A"]


def _pstn() -> str:
    return "+1" + "".join(random.choice("0123456789") for _ in range(10))


def _when(now: datetime) -> datetime:
    """A timestamp in the last 14 days, weighted to weekday business hours."""
    day = now - timedelta(days=random.randint(0, 13))
    # Skew toward weekdays.
    if day.weekday() >= 5 and random.random() < 0.7:
        day -= timedelta(days=2)
    hour = random.choices(
        range(24),
        weights=[1, 1, 1, 1, 1, 1, 2, 4, 8, 10, 10, 9, 7, 9, 10, 9, 7, 5, 3, 2, 2, 1, 1, 1],
    )[0]
    return day.replace(hour=hour, minute=random.randint(0, 59),
                       second=random.randint(0, 59), microsecond=0)


def main() -> int:
    argv = sys.argv[1:]
    calls = 2000
    if "--calls" in argv:
        i = argv.index("--calls")
        calls = int(argv[i + 1])
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("--")]

    with session_scope() as session:
        directory = Path(args[0]) if args else Path(settings_store.load().cdr_dir)
        phones = [
            (name, dn, ip)
            for name, dn, ip in session.execute(
                select(Phone.device_name, Phone.directory_number, Phone.ip_address)
                .where(Phone.device_name.like("SEP%"))
            ).all()
            if dn
        ]

    if len(phones) < 5:
        print("Need at least 5 SEP phones with DNs in the inventory. "
              "Run a collection first.")
        return 1

    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    epoch = lambda dt: int(dt.replace(tzinfo=timezone.utc).timestamp())  # noqa: E731

    cdr_rows: list[list] = []
    cmr_rows: list[list] = []
    leg_seq = 900000
    call_seq = 500000

    def new_leg() -> int:
        nonlocal leg_seq
        leg_seq += 1
        return leg_seq

    def cmr_for(leg_id: int, dev: str, dn: str, poor: bool) -> None:
        if poor:
            mos = round(random.uniform(2.1, 3.3), 1)
            jitter, latency, lost = random.randint(30, 120), random.randint(120, 280), random.randint(40, 400)
        else:
            mos = round(random.uniform(3.9, 4.4), 1)
            jitter, latency, lost = random.randint(2, 25), random.randint(20, 90), random.randint(0, 20)
        sent = random.randint(1500, 9000)
        cmr_rows.append([
            2, 1, call_seq, leg_id, dev, dn, sent, sent - lost, lost, jitter, latency, mos,
        ])

    for _ in range(calls):
        call_seq += 1
        scenario = random.choices(
            ["internal", "outbound", "inbound"], weights=[55, 25, 20]
        )[0]
        answered = random.random() < 0.80
        start = _when(now)
        ring = random.randint(2, 14)
        duration = random.randint(20, 1400) if answered else 0
        connect = start + timedelta(seconds=ring) if answered else None
        disconnect = (connect + timedelta(seconds=duration)) if connect else start + timedelta(seconds=ring)
        # Q.850: 16 normal clearing; else busy/no-answer/rejected.
        cause = 16 if answered else random.choice([17, 18, 19, 21])

        a = random.choice(phones)
        if scenario == "internal":
            b = random.choice(phones)
            orig_dev, dest_dev = a[0], b[0]
            calling, called = a[1], b[1]
            orig_ip, dest_ip = a[2] or "", b[2] or ""
        elif scenario == "outbound":
            orig_dev, dest_dev = a[0], random.choice(TRUNKS)
            calling, called = a[1], _pstn()
            orig_ip, dest_ip = a[2] or "", ""
        else:  # inbound
            orig_dev, dest_dev = random.choice(TRUNKS), a[0]
            calling, called = _pstn(), a[1]
            orig_ip, dest_ip = "", a[2] or ""

        orig_leg, dest_leg = new_leg(), new_leg()
        cdr_rows.append([
            1, 1, call_seq, orig_leg, dest_leg,
            epoch(start), epoch(connect) if connect else 0, epoch(disconnect),
            calling, called, called, orig_dev, dest_dev, orig_ip, dest_ip,
            cause, cause, duration,
        ])

        # Quality (CMR) for answered legs whose endpoint is a phone.
        if answered:
            poor = random.random() < 0.12
            if orig_dev.startswith("SEP"):
                cmr_for(orig_leg, orig_dev, calling, poor)
            if dest_dev.startswith("SEP"):
                cmr_for(dest_leg, dest_dev, called, poor and random.random() < 0.6)

        # ~15% of answered internal calls get a transfer leg (cradle-to-grave).
        if answered and scenario == "internal" and random.random() < 0.15:
            c = random.choice(phones)
            t_orig, t_dest = new_leg(), new_leg()
            t_start = disconnect
            t_dur = random.randint(20, 600)
            cdr_rows.append([
                1, 1, call_seq, t_orig, t_dest,
                epoch(t_start), epoch(t_start + timedelta(seconds=2)),
                epoch(t_start + timedelta(seconds=2 + t_dur)),
                called, c[1], c[1], dest_dev, c[0], dest_ip, c[2] or "",
                16, 16, t_dur,
            ])
            if c[0].startswith("SEP"):
                cmr_for(t_dest, c[0], c[1], random.random() < 0.1)

    ts = now.strftime("%Y%m%d%H%M%S")
    cdr_path = directory / f"cdr_Voxa_01_{ts}_000001.csv"
    cmr_path = directory / f"cmr_Voxa_01_{ts}_000001.csv"

    with cdr_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CDR_FIELDS)
        w.writerow(CDR_TYPES)
        w.writerows(cdr_rows)
    with cmr_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CMR_FIELDS)
        w.writerow(CMR_TYPES)
        w.writerows(cmr_rows)

    print(f"Wrote {len(cdr_rows)} CDR legs -> {cdr_path.name}")
    print(f"Wrote {len(cmr_rows)} CMR quality rows -> {cmr_path.name}")
    print(f"Directory: {directory}")
    print(f"Now ingest: python scripts/ingest_cdr.py {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
