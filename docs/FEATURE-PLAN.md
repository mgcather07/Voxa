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

## Phase 3 — E911 / location mapping  ⬜
Call Telemetry leans hard on this; Voxa roadmap #5. We already discover
switch/port/subnet — the hard half. Writes only to Voxa's own DB.

- ⬜ `Location` model (name, dispatchable address, notes)
- ⬜ Mapping rules: switch/port and/or subnet → location
- ⬜ Resolve per-phone location (at sync or on read)
- ⬜ Locations page (list + add/edit mappings)
- ⬜ Phone 360 shows resolved location
- ⬜ E911 CSV export

## Phase 4 — Reporting upgrades  ⬜
Call Telemetry's report templates + exports. *Deps: openpyxl (XLSX), and PDF
lib if we do PDF — ask when we start.*

- ⬜ XLSX export
- ⬜ Named templates: EoL priority · coverage gaps · by-site · by-model
- ⬜ Reports page to pick + export
- ⬜ Optional: scheduled report to SFTP

## Phase 5 — CDR/CMR ingest (call analytics)  ⬜
Call Telemetry's Call Analytics; Voxa roadmap #3. The big one — phased inside.
*Dep: an SFTP endpoint or a watched drop directory.*

- ⬜ Landing point for CUCM billing CSVs (SFTP or drop dir)
- ⬜ Parse CDR (call volume per device) and CMR (MOS / quality)
- ⬜ Call-record / aggregate schema
- ⬜ Phone 360: per-device call volume + last call ("don't replace unused")
- ⬜ Dashboard: utilization + quality summaries

## Phase 6 — Enablers  ⬜
- ⬜ AD/LDAP auth (finish the stubbed `LdapAuthBackend`) — *dep: ldap3*
- ⬜ Multi-cluster (cluster model; cluster-aware collection + filters)
- ⬜ SNMP PoE polling (real draw vs class ceiling) — *dep: pysnmp*, roadmap #2
- ⬜ CSV device import (Call Telemetry's "CSV lists" discovery)
