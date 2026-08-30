"""Seed realistic mock phones so you can run the app without CUCM.

    python scripts/mock_data.py            # 400 phones
    python scripts/mock_data.py 2500       # more
    python scripts/mock_data.py --wipe     # clear the table first
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import get_catalog  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Phone, SyncRun  # noqa: E402

SITES = {
    "HQ": ("DP_HQ_01", 12),
    "PLANT1": ("DP_PLANT1_01", 8),
    "PLANT2": ("DP_PLANT2_01", 6),
    "WAREHOUSE": ("DP_WAREHOUSE_01", 4),
    "REMOTE": ("DP_REMOTE_01", 3),
}

# Weighted toward a realistic aging fleet: mostly 7900s, some 8800s.
MODEL_MIX = [
    ("Cisco 7962", 26), ("Cisco 7942", 18), ("Cisco 7965", 9),
    ("Cisco 7941", 8),  ("Cisco 7961", 8),  ("Cisco 7911", 7),
    ("Cisco 7975", 4),  ("Cisco 7945", 3),  ("Cisco 6921", 3),
    ("Cisco 8841", 6),  ("Cisco 8851", 3),  ("Cisco 8845", 2),
    ("Cisco 7841", 2),  ("Cisco 8832", 1),
]

DEPARTMENTS = [
    "Front Desk", "Accounting", "Shipping", "Maintenance", "Nurse Station",
    "Break Room", "Conference", "Security", "Dispatch", "QA Lab",
    "Purchasing", "HR", "Line 1", "Line 2", "Shop Office", "Guard Shack",
]

FIRMWARE = {
    "79": ["SCCP42.9-4-2SR3-1S", "SCCP45.9-4-2SR3-1S", "SIP42.9-4-2SR3-1S"],
    "69": ["SCCP69xx.9-4-1-3"],
    "78": ["sip78xx.14-2-1-0001-131"],
    "88": ["sip88xx.14-2-1-0001-131"],
}


def weighted_models(n: int) -> list[str]:
    pool: list[str] = []
    for model, weight in MODEL_MIX:
        pool.extend([model] * weight)
    return [random.choice(pool) for _ in range(n)]


def mac() -> str:
    return "SEP" + "".join(random.choice("0123456789ABCDEF") for _ in range(12))


def firmware_for(model: str) -> str:
    prefix = "".join(c for c in model if c.isdigit())[:2]
    return random.choice(FIRMWARE.get(prefix, FIRMWARE["79"]))


def main() -> None:
    args = [a for a in sys.argv[1:]]
    wipe = "--wipe" in args
    count = next((int(a) for a in args if a.isdigit()), 400)

    init_db()
    catalog = get_catalog()
    now = datetime.now(timezone.utc)

    with session_scope() as session:
        if wipe:
            session.query(Phone).delete()
            session.query(SyncRun).delete()

        site_names = list(SITES)
        weights = [SITES[s][1] for s in site_names]
        models = weighted_models(count)
        dn_base = 2000

        for i, model_raw in enumerate(models):
            site = random.choices(site_names, weights=weights)[0]
            device_pool, closets = SITES[site]
            info = catalog.lookup(model_raw)

            registered = random.random() > 0.06
            ip = (
                f"10.{site_names.index(site) + 10}."
                f"{random.randint(1, 6)}.{random.randint(2, 250)}"
            )
            scraped = registered and random.random() > 0.18
            switch = (
                f"{site.lower()}-acc-{random.randint(1, closets):02d}"
                if scraped else None
            )

            phone = Phone(
                device_name=mac(),
                pkid=f"mock-{i:06d}",
                description=(
                    f"{random.choice(DEPARTMENTS)} "
                    f"{random.randint(100, 999)} - {site}"
                ),
                model_raw=model_raw,
                model_key=info.key,
                protocol=random.choice(["SCCP", "SIP"]),
                device_pool=device_pool,
                site=site,
                configured_load=firmware_for(model_raw),
                directory_number=str(dn_base + i),
                registration_status="Registered" if registered else "UnRegistered",
                status_reason=None if registered else "Unknown",
                ip_address=ip if registered else None,
                active_load=firmware_for(model_raw) if registered else None,
                cm_node=random.choice(["cucm-sub1", "cucm-sub2"]),
                serial_number=(
                    "FCH" + "".join(
                        random.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ")
                        for _ in range(9)
                    )
                ) if scraped else None,
                hardware_revision=f"{random.randint(1, 6)}.0" if scraped else None,
                switch_name=switch,
                switch_port=(
                    f"GigabitEthernet1/0/{random.randint(1, 48)}"
                    if scraped else None
                ),
                switch_ip=(
                    f"10.{site_names.index(site) + 10}.0.{random.randint(2, 20)}"
                    if scraped else None
                ),
                vlan_id=str(random.choice([110, 120, 130])) if scraped else None,
                web_reachable=scraped,
                family=info.family,
                generation=info.generation,
                lifecycle=info.lifecycle,
                poe_class=info.poe_class,
                poe_watts=info.poe_watts,
                replacement_key=info.replacement_key,
                replacement_name=info.replacement_name,
                replacement_poe_watts=info.replacement_poe_watts,
                swap_status=random.choices(
                    ["not_started", "planned", "ordered", "installed"],
                    weights=[80, 12, 5, 3],
                )[0],
                first_seen=now,
                last_seen=now,
            )
            session.add(phone)

        session.add(
            SyncRun(
                started_at=now,
                finished_at=now,
                status="success",
                stage="done",
                axl_count=count,
                ris_count=count,
                web_count=sum(1 for m in models if True) // 2,
                created=count,
                updated=0,
            )
        )

    print(f"Seeded {count} mock phones across {len(SITES)} sites.")


if __name__ == "__main__":
    main()
