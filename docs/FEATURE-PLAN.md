# Voxa feature plan — read-only Call Telemetry parity

Inspired by [calltelemetry.com](https://calltelemetry.com), scoped to Voxa's
one hard rule: **Voxa is read-only. It never writes to CUCM or phones.** So the
control side of Call Telemetry — call blocking, the policy engine, spam
redirect, live remote control, factory reset/ITL, greeting injection — is
deliberately **out of scope**. Voxa's lane is visibility, inventory, discovery,
planning, reporting, and alerting on data it *reads*.

We build one phase at a time; each phase is a shippable increment. Dependencies
are flagged where a phase needs one (per CLAUDE.md, we ask before adding any).

Status legend: ⬜ todo · 🔶 in progress · ✅ done

---

## Phase 1 — Phone 360 device workspace  ✅
Call Telemetry's flagship "Phone 360" view, read-only. Click any phone → one
page with everything Voxa knows about it. No new data, no new deps.

- ✅ `GET /phones/{id}` detail route
- ✅ Link the device name in the phone table to it
- ✅ Sections: Identity · Registration (RisPort) · Discovery (phone web) ·
     Refresh planning (catalog) · Record/meta
- ✅ Inline swap-status control on the detail page (reuse `_swap_cell`)
- ✅ Per-field empty states ("not collected")

## Phase 2 — Scheduled collection + change history  ✅
Call Telemetry's "last-registered / last-call activity"; Voxa roadmap #1. Adds
the time dimension — the feature that makes the tool sticky after the refresh.
Scheduling is the OS's job (cron / systemd timer calling `scripts/collect.py`),
so no in-app scheduler dependency.

- ✅ Per-phone-per-run snapshot table (`PhoneSnapshot`)
- ✅ Record snapshots inside `sync.run_sync()`
- ✅ Scheduled collection via `scripts/collect.py` + systemd timer / cron
- ✅ "What changed" view: appeared / dropped / moved switch-port / reg change /
     firmware change, with a run picker
- ✅ Phone 360: that phone's change timeline
- ✅ Fleet-health trend on the History page
- ✅ `scripts/mock_data.py` fabricates 6 runs of history

## Phase 3 — E911 / location mapping  ✅
Call Telemetry leans hard on this; Voxa roadmap #5. We already discover
switch/port/subnet — the hard half. Writes only to Voxa's own DB.

- ✅ `Location` model (name, dispatchable address, notes)
- ✅ Mapping rules: switch-name prefix and/or subnet CIDR → location
- ✅ Resolve per-phone location on read (`app/locations.py`, most-specific wins)
- ✅ Locations page (list + add location + add/remove rules + coverage %)
- ✅ Phone 360 shows resolved location
- ✅ E911 CSV export (`/locations/e911.csv`)

## Phase 4 — Reporting upgrades  ✅
Call Telemetry's report templates + exports. Delivered with **no new
dependencies**: a tiny hand-rolled XLSX writer, and print-to-PDF via a print
stylesheet instead of a PDF library.

- ✅ XLSX export (`app/exports.py`, dependency-free)
- ✅ Named templates: replace-first (EoL) · discovery gaps · by-site · by-model
- ✅ `/reports` page: view on screen, CSV / Excel export, print-to-PDF
- ✅ Scheduled report delivery: `scripts/report.py` dumps files for cron + OS SFTP

## Phase 5 — CDR/CMR ingest (call analytics)  ✅
Call Telemetry's Call Analytics; Voxa roadmap #3. Read from a drop directory
(the OS/SFTP lands files; the app has no SFTP client). Aggregates, not raw
records — enough to answer "which phones does nobody use?".

- ✅ Drop-directory landing (`CDR_DIR`), no SFTP client in the app
- ✅ Tolerant CDR/CMR parser (`app/cdr.py`) — call volume + MOS, per device
- ✅ `CallStat` aggregate schema; `scripts/ingest_cdr.py` for cron
- ✅ Phone 360: call volume / talk time / last call / MOS, or "retire not replace"
- ✅ Dashboard call-activity card + an "unused phones" report

## Phase 6 — Enablers  ⬜
- ⬜ AD/LDAP auth (finish the stubbed `LdapAuthBackend`) — *dep: ldap3*
- ⬜ Multi-cluster (cluster model; cluster-aware collection + filters)
- ⬜ SNMP PoE polling (real draw vs class ceiling) — *dep: pysnmp*, roadmap #2
- ⬜ CSV device import (Call Telemetry's "CSV lists" discovery)
