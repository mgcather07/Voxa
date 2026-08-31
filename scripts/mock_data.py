"""Seed realistic mock phones so you can run the app without CUCM.

    python scripts/mock_data.py            # 400 phones
    python scripts/mock_data.py 2500       # more
    python scripts/mock_data.py --wipe     # clear the table first
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import get_catalog  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    CallQuality,
    CallRecord,
    CallStat,
    Location,
    LocationRule,
    Phone,
    PhoneSnapshot,
    SwitchPoll,
    SyncRun,
)

# How many historical collection runs to fabricate, so change history and the
# fleet trend have something to show without a live CUCM.
HISTORY_RUNS = 6
OLDER_FIRMWARE = "SCCP42.9-3-1SR4-1S"

SITES = {
    "HQ": ("DP_HQ_01", 12),
    "PLANT1": ("DP_PLANT1_01", 8),
    "PLANT2": ("DP_PLANT2_01", 6),
    "WAREHOUSE": ("DP_WAREHOUSE_01", 4),
    "REMOTE": ("DP_REMOTE_01", 3),
}

# Two clusters, so the multi-cluster UI has something to show.
SITE_CLUSTER = {
    "HQ": "cucm-east", "PLANT1": "cucm-east",
    "PLANT2": "cucm-west", "WAREHOUSE": "cucm-west", "REMOTE": "cucm-west",
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
            session.query(PhoneSnapshot).delete()
            session.query(CallQuality).delete()
            session.query(CallRecord).delete()
            session.query(CallStat).delete()
            session.query(SwitchPoll).delete()
            session.query(LocationRule).delete()
            session.query(Location).delete()
            session.query(Phone).delete()
            session.query(SyncRun).delete()

        created_phones: list[Phone] = []
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
                cluster=SITE_CLUSTER.get(site, "cucm-east"),
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
            created_phones.append(phone)

        _seed_history(session, created_phones, now)
        _seed_locations(session)
        _seed_calls(session, created_phones, now)
        _seed_switch_polls(session, created_phones, now)

    print(f"Seeded {count} mock phones across {len(SITES)} sites.")


def _seed_switch_polls(session, phones: list[Phone], now: datetime) -> None:
    """Fake SNMP results per switch (real draw + budget), so the PoE page can
    show real numbers next to the class-ceiling estimate."""
    # phones per switch -> a plausible real draw
    per_switch: dict[str, int] = {}
    for p in phones:
        if p.switch_name:
            per_switch[p.switch_name] = per_switch.get(p.switch_name, 0) + 1
    for switch, ports in per_switch.items():
        budget = random.choice([370.0, 740.0, 1440.0])
        # real draw roughly 5-9 W/phone port plus some non-phone load, capped.
        draw = min(budget * 0.95, ports * random.uniform(5.0, 9.0) + random.uniform(0, 60))
        session.add(
            SwitchPoll(
                switch_name=switch,
                available_watts=round(budget, 1),
                used_watts=round(draw, 1),
                polled_at=now,
            )
        )


def _quality(leg_id: int, phone: Phone, talk: int) -> CallQuality:
    return CallQuality(
        leg_id=leg_id,
        device=phone.device_name,
        directory_number=phone.directory_number,
        mos=round(random.uniform(3.5, 4.5), 2),
        jitter_ms=round(random.uniform(1, 45), 1),
        latency_ms=round(random.uniform(8, 165), 1),
        packets_sent=int(talk * 50),
        packets_lost=int(talk * 50 * random.uniform(0, 0.02)),
    )


def _bump(stats: dict, device: str, *, inbound=0, outbound=0, secs=0, when=None, mos=None):
    s = stats.setdefault(
        device,
        {"total": 0, "in": 0, "out": 0, "secs": 0, "last": None,
         "mos_sum": 0.0, "mos_count": 0},
    )
    s["total"] += inbound + outbound
    s["in"] += inbound
    s["out"] += outbound
    s["secs"] += secs
    if when and (s["last"] is None or when > s["last"]):
        s["last"] = when
    if mos is not None:
        s["mos_sum"] += mos
        s["mos_count"] += 1


def _seed_calls(session, phones: list[Phone], now: datetime) -> None:
    """Generate realistic CDR legs + CMR quality, then derive CallStat.

    ~75% of registered phones make/receive calls; the rest stay unused so the
    'retire, don't replace' report still finds them. ~12% of answered internal
    calls get a transfer leg, so cradle-to-grave has multi-leg calls to show.
    """
    active = [
        p for p in phones
        if p.directory_number and p.registration_status == "Registered"
        and random.random() < 0.75
    ]
    if len(active) < 2:
        return
    externals = [f"+1615{random.randint(2000000, 9999999)}" for _ in range(40)]
    stats: dict[str, dict] = {}
    call_id = 100000
    leg_id = 500000
    n_calls = len(phones) * 6

    for _ in range(n_calls):
        call_id += 1
        caller = random.choice(active)
        internal = random.random() < 0.78
        if internal:
            callee = random.choice(active)
            if callee is caller:
                continue
            callee_num, callee_dev, callee_ip = (
                callee.directory_number, callee.device_name, callee.ip_address
            )
        else:
            callee, callee_dev, callee_ip = None, None, None
            callee_num = random.choice(externals)
        cmid = 1 if caller.cluster == "cucm-east" else 2

        start = now - timedelta(
            days=random.randint(0, 14), minutes=random.randint(0, 1440),
            seconds=random.randint(0, 59),
        )
        answered = random.random() < 0.82
        ring = random.randint(2, 25)
        talk = random.randint(20, 1800) if answered else 0
        connect = start + timedelta(seconds=ring) if answered else None
        disconnect = (
            connect + timedelta(seconds=talk) if answered
            else start + timedelta(seconds=ring)
        )
        end_cause = 16 if answered else random.choice([17, 18, 19, 20, 21])
        olid, dlid = leg_id, leg_id + 1
        leg_id += 2

        session.add(CallRecord(
            call_mgr_id=cmid, call_id=call_id, orig_leg_id=olid, dest_leg_id=dlid,
            orig_device=caller.device_name, dest_device=callee_dev,
            calling_number=caller.directory_number,
            original_called=callee_num, final_called=callee_num,
            orig_ip=caller.ip_address, dest_ip=callee_ip,
            orig_time=start, connect_time=connect, disconnect_time=disconnect,
            duration=talk, orig_cause=16 if answered else 0, dest_cause=end_cause,
        ))

        caller_mos = None
        if answered:
            cq = _quality(olid, caller, talk)
            session.add(cq)
            caller_mos = cq.mos
            if internal and callee is not None:
                cq2 = _quality(dlid, callee, talk)
                session.add(cq2)
                _bump(stats, callee.device_name, inbound=1, secs=talk,
                      when=start, mos=cq2.mos)
        elif internal and callee is not None:
            _bump(stats, callee.device_name, inbound=1, when=start)
        _bump(stats, caller.device_name, outbound=1, secs=talk, when=start,
              mos=caller_mos)

        # Transfer: a second leg on the same call to a third party.
        if answered and internal and callee is not None and random.random() < 0.12:
            third = random.choice(active)
            if third is caller or third is callee:
                continue
            t_start = connect + timedelta(seconds=random.randint(5, max(6, talk)))
            t_talk = random.randint(10, 600)
            olid2, dlid2 = leg_id, leg_id + 1
            leg_id += 2
            session.add(CallRecord(
                call_mgr_id=cmid, call_id=call_id,
                orig_leg_id=olid2, dest_leg_id=dlid2,
                orig_device=callee.device_name, dest_device=third.device_name,
                calling_number=callee_num,
                original_called=third.directory_number,
                final_called=third.directory_number,
                orig_ip=callee_ip, dest_ip=third.ip_address,
                orig_time=t_start, connect_time=t_start + timedelta(seconds=2),
                disconnect_time=t_start + timedelta(seconds=t_talk),
                duration=t_talk, orig_cause=16, dest_cause=16,
            ))
            cq3 = _quality(dlid2, third, t_talk)
            session.add(cq3)
            _bump(stats, third.device_name, inbound=1, secs=t_talk,
                  when=t_start, mos=cq3.mos)

    for device, s in stats.items():
        session.add(CallStat(
            device_name=device, total_calls=s["total"],
            inbound_calls=s["in"], outbound_calls=s["out"],
            total_seconds=s["secs"], last_call_at=s["last"],
            mos_sum=round(s["mos_sum"], 2), mos_count=s["mos_count"],
        ))


SITE_ADDRESSES = {
    "HQ": "100 Main St, Nashville TN 37201",
    "PLANT1": "200 Industrial Way, Memphis TN 38103",
    "PLANT2": "450 Factory Rd, Chattanooga TN 37402",
    "WAREHOUSE": "12 Dock Ave, Knoxville TN 37902",
    "REMOTE": "Remote / VPN — no fixed address",
}


def _seed_locations(session) -> None:
    """One dispatchable location per site, mapped by its switch-name prefix."""
    for site in SITES:
        loc = Location(name=f"{site} — main", address=SITE_ADDRESSES.get(site))
        session.add(loc)
        session.flush()
        session.add(
            LocationRule(
                location_id=loc.id, match_type="switch", pattern=f"{site.lower()}-acc"
            )
        )


def _older_firmware(load: str | None) -> str:
    if load and load != OLDER_FIRMWARE:
        return OLDER_FIRMWARE
    return "SCCP42.9-2-1SR1-1S"


def _seed_history(session, phones: list[Phone], now: datetime) -> None:
    """Fabricate HISTORY_RUNS collection runs with per-phone snapshots.

    The newest run mirrors the current fleet exactly; earlier runs have lower
    discovery coverage (so the trend improves over time), and the run just
    before the newest carries deliberate, countable changes — a handful of
    phones that appeared, moved switch port, re-registered, or took a firmware
    bump — so the "what changed" view has real content.
    """
    n = len(phones)
    late_appear = set(phones[: min(6, n)])          # only in the newest run
    moved = set(phones[6:16])                        # moved switch port last run
    reg_flip = set(phones[16:26])                    # were unregistered last run
    fw_bump = set(phones[26:36])                     # firmware bumped last run

    for r in range(HISTORY_RUNS):
        newest = r == HISTORY_RUNS - 1
        frac = (r + 1) / HISTORY_RUNS
        run_time = now - timedelta(days=(HISTORY_RUNS - 1 - r))

        run = SyncRun(
            started_at=run_time,
            finished_at=run_time,
            status="success",
            stage="done",
        )
        session.add(run)
        session.flush()  # need run.id

        present = 0
        with_switch = 0
        for p in phones:
            if not newest and p in late_appear:
                continue  # hadn't appeared yet
            present += 1

            reg = p.registration_status
            ip = p.ip_address
            switch = p.switch_name
            port = p.switch_port
            load = p.active_load
            has_serial = p.serial_number is not None

            if not newest:
                # Coverage was worse further back in time.
                if switch and random.random() > (0.55 + 0.45 * frac):
                    switch, port = None, None
                if has_serial and random.random() > (0.55 + 0.45 * frac):
                    has_serial = False
                if reg == "Registered" and random.random() > (0.9 + 0.1 * frac):
                    reg, ip = "UnRegistered", None

            # Deliberate, countable changes between the last two runs.
            if r == HISTORY_RUNS - 2:
                if p in moved and switch:
                    port = "GigabitEthernet2/0/1"
                if p in reg_flip and reg == "Registered":
                    reg, ip = "UnRegistered", None
                if p in fw_bump and load:
                    load = _older_firmware(load)

            if switch:
                with_switch += 1

            session.add(
                PhoneSnapshot(
                    sync_run_id=run.id,
                    device_name=p.device_name,
                    registration_status=reg,
                    ip_address=ip,
                    switch_name=switch,
                    switch_port=port,
                    active_load=load,
                    has_serial=has_serial,
                    captured_at=run_time,
                )
            )

        run.axl_count = present
        run.ris_count = present
        run.web_count = with_switch
        run.created = present if r == 0 else len(late_appear) if newest else 0
        run.updated = 0 if r == 0 else present


if __name__ == "__main__":
    main()
