# Roadmap

Three features, in the order they were prioritized. Each section is a design
sketch, not a spec — enough that whoever picks it up doesn't start from a
blank page, loose enough that they can make better decisions with the code in
front of them.

---

## 1. Scheduled collection + change history

**Why first.** Today the data is only as fresh as the last time someone
clicked a button, and the app only knows the present. Adding history is what
turns a migration spreadsheet into an operations tool the team keeps using
after the swap is finished: *which phones dropped off the network last night,
what moved to a different switch port, what appeared that nobody told us
about.*

### Scheduling

Run the sync from the app process rather than a system cron, so the schedule
travels with the deployment:

```python
# APScheduler, started in main.py's startup hook
scheduler.add_job(run_sync, CronTrigger(hour=2, minute=30), id="nightly")
```

Add `apscheduler` to requirements. Two things to get right:

- **Only one worker may schedule.** The Docker image runs two uvicorn workers,
  so both would fire the job. Either run the scheduler in a separate container
  from the same image (`command: python -m app.scheduler`), or gate it behind
  a Postgres advisory lock. The separate container is cleaner and easier to
  reason about.
- The existing `SyncRun.status == "running"` guard in `main.trigger_sync`
  already prevents a manual click from colliding with the scheduled run.
  Reuse it, don't reinvent it.

Make the schedule configurable (`SYNC_CRON=30 2 * * *`) and let an empty value
disable it.

### Change history

New table, one row per phone per sync, holding only the fields worth tracking:

```python
class PhoneSnapshot(Base):
    __tablename__ = "phone_snapshots"
    id, phone_id (FK), sync_run_id (FK), captured_at
    registration_status, ip_address, active_load
    switch_name, switch_port, device_pool, model_key
```

Rather than storing a row per phone per night forever, **write a snapshot only
when a tracked field differs from the last one**. A stable fleet then costs
almost nothing, and the table stays readable. Also record disappearance: a
phone in the database that no source returned this run is an event
(`Phone.missing_since`), not a row to silently leave stale.

Storage sanity check: 5,000 phones × a genuinely eventful day is a few
thousand rows. This will not be your problem.

### What to build on it

- A **Changes** page: what changed since a given date, filterable by kind
  (moved port, dropped registration, new phone, firmware changed).
- "New since last run" and "missing since" counts on the dashboard.
- Per-phone timeline on a phone detail page — which does not exist yet and
  probably should, as part of this work.

Genuinely useful during the swap: a phone that moves switch ports overnight is
usually someone unplugging a phone to plug in something else.

---

## 2. SNMP polling of access switches

**Why.** The PoE page currently estimates from IEEE class ceilings, because
that is all the phones can tell us. The switch knows the truth: actual draw
per port, total available power, and what else is on the budget. It also knows
which port a phone is on even when that phone's web server is disabled, which
would close the coverage gap on the two most valuable columns.

### Approach

Add `pysnmp` (or shell out to `snmpwalk` if the ops team already manages
credentials for it — worth asking before choosing). Poll each switch
discovered via CDP, plus any added manually.

MIBs that matter:

- `POWER-ETHERNET-MIB` — `pethPsePortAdminEnable`, `pethPsePortPowerClass`,
  `pethMainPseUsagePower`, `pethMainPsePower` (available budget)
- `CISCO-POWER-ETHERNET-EXT-MIB` — `cpeExtPsePortPwrConsumption`, the real
  per-port draw in milliwatts
- `BRIDGE-MIB` / `CISCO-VLAN-MEMBERSHIP-MIB` — MAC address table, to map a
  phone's MAC to a port without touching the phone
- `IF-MIB` — `ifName`, to turn an ifIndex into "GigabitEthernet1/0/12"

### Schema

```python
class Switch(Base):
    name, ip_address, model, ios_version, site
    poe_available_w, poe_used_w, last_polled

class SwitchPort(Base):
    switch_id (FK), if_index, if_name
    poe_admin, poe_class, poe_draw_mw
    mac_address, vlan
```

Then join phones to ports on MAC (`Phone.device_name` is `SEP` + the MAC) and
prefer measured draw over class estimate on the PoE page, showing both so the
gap is visible.

### Gotchas

- **Credentials.** SNMPv3 with auth+priv, not v2c community strings. Store
  them in the environment like the CUCM credentials, never in the database.
  This is the piece most likely to need a security conversation — start it
  early.
- Read-only views only. Same rule as CUCM: this tool does not write to
  network devices.
- Poll switches, not phones — a few hundred devices instead of thousands.
- Stack members report per-member power budgets. Don't sum them into one
  number and call it the switch's capacity.

---

## 3. CDR/CMR ingest

**Why.** The refresh plan currently assumes every phone deserves a
replacement. Call detail records answer the question that saves real money:
*which of these phones has anybody actually used in the last 90 days?* A
warehouse phone with four calls in a quarter is a candidate for removal, not
replacement.

### How the data arrives

You do not poll for CDR. CUCM pushes it. In **Serviceability → Tools → CDR
Management**, add a Billing Application Server pointing at this VM over SFTP.
CUCM then writes CSV files every minute or so.

That means running an SFTP endpoint:

- A container in the stack with a restricted user and a drop directory, or
- `openssh-server` on the VM with a chrooted `cdr` user who can only write to
  `/var/spool/cdr`.

Then a watcher moves each finished file into a parse queue. Do not parse a
file while CUCM is still writing it — process on rename, or ignore files
modified in the last 60 seconds.

### Parsing

CDR and CMR are CSV with two header rows: field names, then field types.
Both must be skipped. Fields that matter to us:

- CDR: `globalCallID_callId`, `dateTimeConnect`, `dateTimeDisconnect`,
  `callingPartyNumber`, `originalCalledPartyNumber`, `duration`,
  `origDeviceName`, `destDeviceName`, `origCause_value`, `destCause_value`
- CMR: `globalCallID_callId`, `deviceName`, `jitter`, `latency`,
  `pktsLost`, `pktsRcvd`, and the MOS fields on newer releases

Join CDR to CMR on `globalCallID_callId`.

### Schema and retention

```python
class CallRecord(Base):
    call_id, connected_at, duration_s
    calling_number, called_number
    orig_device_name, dest_device_name       # index these - the join to phones
    orig_cause, dest_cause

class CallQuality(Base):
    call_id (FK), device_name, jitter_ms, latency_ms, pkts_lost, pkts_rcvd
```

**Decide retention before turning this on.** A busy cluster generates hundreds
of thousands of records a month, and this is the feature that turns a 1 GB
database into a 50 GB one. Options, roughly in order of preference:

1. Keep 90 days of detail, aggregate older data into a per-device monthly
   rollup, delete the detail. This serves the actual question.
2. Partition `call_records` by month so deletion is a `DROP TABLE`.
3. Keep everything and revisit the disk request. Least effort now, most later.

Then add the column that pays for all of it: **calls in the last 90 days**, on
the phone table and in the CSV export, so "replace" and "remove" become
different decisions.

### Gotchas

- The parse is CPU-bound and bursty. Run it out of the request path — a
  worker container, or the same scheduler process added in item 1.
- Records arrive continuously and can be re-sent. Make the insert idempotent
  on `call_id` + `device_name`.
- CDR contains who called whom. It is more sensitive than anything else in
  this app. Restrict it to admin accounts, and check whether your retention
  policy or works council has something to say before you store it.

---

## Also worth doing, unprioritized

- **Alembic migrations.** `create_all()` won't alter an existing table, and
  the first schema change on a database with real swap-tracking data in it
  will hurt. See the note in `docs/DEPLOY.md`.
- **Active Directory login.** The seam exists (`auth.LdapAuthBackend`); the
  bind sequence is written out in its docstring. Keep local accounts working
  as a break-glass path.
- **Phone detail page.** Currently everything is a table row. A per-device
  page is the natural home for the change timeline in item 1.
- **Tests.** Fixture-based tests against recorded SOAP responses would make
  the parsers safe to refactor. Record the fixtures from a real cluster once
  and scrub the descriptions.
