# Voxa — Project Handoff & Status

> **Purpose of this file:** the single "where are we / what's going on" doc.
> Read this first when picking the project up on another machine. Last
> updated **2026-08-31**.

> 🔌 **Connecting to a live CUCM cluster?** Follow
> **[docs/CONNECT-LIVE.md](docs/CONNECT-LIVE.md)** — it deploys Voxa in Docker
> and points it at a real (CUCM 15) publisher over VPN, step by step. That path
> is proven working (see the fixes below).

---

## What Voxa is

**Voxa** is an internal, **read-only** Call Telemetry web app for Cisco Unified
Communications Manager. It never writes to CUCM or phones — every call is a
read. Two halves, both live:

- **Inventory & refresh planning** — pulls a live phone inventory (AXL +
  RisPort + each phone's web server), turns it into a refresh plan: what phones
  exist, what each maps to, the switch/port each sits on, and the PoE budget per
  closet. Dashboard, phone table, Phone 360, refresh plan, PoE budget, E911
  locations, model catalog, CSV/XLSX export.
- **Call telemetry** — ingests CUCM CDR/CMR files into Calls (cradle-to-grave
  search + SIP ladder), Analytics (volume, busy hour, top talkers, MOS,
  disconnect causes), and Capacity (concurrency, Erlangs, BHCA, trunk
  utilization). Read from a drop directory — no SFTP client in the app.

- **Product docs:** [README.md](README.md)
- **Engineering conventions:** [CLAUDE.md](CLAUDE.md) (read before changing code)
- **What's built, phase by phase:** [docs/FEATURE-PLAN.md](docs/FEATURE-PLAN.md)
- **Deep docs:** [docs/](docs/) — ARCHITECTURE, CUCM-SETUP, DEPLOY, CONNECT-LIVE, ROADMAP

---

## Current status (2026-08-31)

- ✅ **Fully built** — inventory/refresh/PoE **and** the full call-telemetry
  suite (Calls/Analytics/Capacity), model catalog editor, reports, read API +
  webhooks, LDAP/SNMP options. See FEATURE-PLAN.md.
- ✅ **Live CUCM 15 connection proven** on the MacBook Pro: Docker dev stack,
  real inventory collected (~1,742 phones), all three CUCM sources answering.
  Getting there required three CUCM-15 compatibility fixes (below).
- ✅ **Call telemetry demonstrated** end-to-end via ingested sample CDR/CMR
  (real device names) — Calls/Analytics/Capacity populate; the SIP ladder shows
  device IPs, the registered CUCM node, and per-leg duration.
- ✅ Pushed to GitHub: **https://github.com/mgcather07/Voxa.git** (`main`).
- ⏳ Not yet done: production deploy to vSphere, a CUCM Billing Application
  Server to push real CDR, auth enabled, HTMX vendored for offline use.

**Stack:** FastAPI + Jinja2 + HTMX + SQLAlchemy. Server-rendered, no JS build
step. Postgres in prod/dev-Docker, SQLite for the quick non-Docker path.
Python 3.12/3.13.

---

## Get it running

### Docker (matches the live-connect path — recommended)

```bash
git clone https://github.com/mgcather07/Voxa.git && cd Voxa
make dev-up            # builds image, starts Postgres + app on :8000
# DO NOT `make dev-seed` if you're about to connect real CUCM — keep it empty.
```

Then follow **[docs/CONNECT-LIVE.md](docs/CONNECT-LIVE.md)**: connect the VPN,
prove the container can reach the publisher on 8443, add the cluster in
**Settings → Add a cluster** (AXL version **15.0**, TLS **off** for a
self-signed cert), and **Collect from CUCM**.

Useful:
```bash
docker compose -f docker-compose.dev.yml logs -f app     # app logs
docker compose -f docker-compose.dev.yml exec app python scripts/test_cucm.py   # CLI probe
make dev-down          # stop    |    make dev-reset  # stop + wipe DB
```

### See the call-telemetry pages without a live CDR feed

```bash
docker compose -f docker-compose.dev.yml exec app python scripts/mock_cdr.py --calls 2200
docker compose -f docker-compose.dev.yml exec app python scripts/ingest_cdr.py
```
Generates CUCM-format CDR/CMR from the collected inventory into `./cdr-inbox`
(mounted as `CDR_DIR`) and folds it in. **Ingest each batch once** — it's
additive with no per-file de-dup (re-running the same files double-counts).
Clear sample call data without touching inventory:
```bash
docker compose -f docker-compose.dev.yml exec -T db psql -U voxa -d voxa -c "TRUNCATE call_records, call_quality, call_stats;"
```

### Quick non-Docker look (SQLite + mock inventory)

`make install && source .venv/bin/activate && cp .env.local.example .env && make mock && make dev`

Login is off locally (`AUTH_DISABLED=true`). Enable it for anything deployed
(`AUTH_DISABLED=false`, then `python scripts/manage.py create-user admin --admin`).

---

## What was fixed / built in the live-connect sessions

**CUCM 15 compatibility (the reason a fresh collect failed at first):**
- **AXL** — the phone SQL used `d.loadinformation`, which doesn't exist on CUCM
  15's `device` table. The per-device firmware override is `specialloadinformation`.
- **RisPort** — `selectCmDeviceExt` rejected a bare `*` in `SelectItems`. This
  release wants an **empty `Item`** to mean "all devices".
- **Switch/port** — 78xx/88xx phones expose the CDP/LLDP neighbour on
  `/PortInformationX`, not `/NetworkConfiguration`. Coverage went 0% → ~36%
  (the rest is phones with Web Access disabled — an operational toggle on the
  Common Phone Profile, not a code issue).

**Features & polish added:**
- **Model catalog editor** (`/catalog`) — admins edit PoE class / lifecycle /
  replacement / verified in the UI; DB overrides the YAML, applies instantly.
  No more editing `config/models.yaml` on the server.
- **Number formatting** — thousands separators (1,742) via a `comma` filter.
- **12-hour clock + friendlier dates** everywhere ("Aug 31, 2026 · 11:46 PM"),
  including the SIP ladder axis and busy-hour labels.
- **SIP ladder enrichment** — device IPs, the registered CUCM node, per-leg
  duration on hang-up.
- **CDR ingest setup** — `cdr-inbox/` drop folder mounted as `CDR_DIR`,
  `scripts/mock_cdr.py` sample generator.

---

## Deployment plan (vSphere)

Target: a Linux VM on the company vSphere estate, behind nginx with TLS.

- **VM provisioning:** clone from IT's golden Linux template (patched, monitored,
  backed up). A custom OVA isn't worth it for a single instance.
- **Run the app:** Docker Compose ([docker-compose.prod.yml](docker-compose.prod.yml))
  if Docker is approved on the server estate, else the systemd unit
  ([deploy/voxa.service](deploy/voxa.service)). Open question: *is Docker allowed
  on server VMs?*
- **Real call data needs a CUCM Billing Application Server** (Cisco Unified
  Serviceability → Tools → CDR Management) doing an **SFTP push** to the VM's
  `CDR_DIR`. This only works where CUCM can reach the target — the VM, not a
  laptop behind the VPN. CDR service params (CDR Enabled Flag, Call Diagnostics
  Enabled) live under **Service Parameters → Cisco CallManager**, not Enterprise
  Parameters.
- Full VM sizing, firewall, TLS, backup, upgrade: [docs/DEPLOY.md](docs/DEPLOY.md).

---

## Ideas / roadmap (all doable read-only)

- [ ] **Cluster status page** — once a cluster is connected, show node health:
      publisher + subscribers (already discovered from `processnode`), per-node
      registered-device counts (RisPort already returns these per `CmNode`), and
      8443 reachability. Foundation: persist a `ClusterNode` table at collection.
- [ ] **Resolve the ladder's CUCM IP to a node name** — the ladder shows the
      registered node (sometimes an IP). With a stored node name↔IP map (same
      `ClusterNode` table), display "IP (nodename)". Shares infra with the status
      page above.
- [ ] **Certificate inventory** — connect to each node's TLS ports (8443 tomcat,
      5061 CallManager SIP, 2443 CAPF, …) and read the served leaf certificate:
      subject, issuer, SAN, valid-from/-to, **days-to-expiry**, self-signed vs
      CA-signed. Pure TLS handshake = fully read-only, no special CUCM API.
      Flag expiring/expired. (Certs not served on a socket — e.g. ITLRecovery —
      would need the PAWS API, which is version-specific; start with served certs.)
- [ ] **MOS/quality in the ladder** — color a lifeline / badge a leg when its
      CMR MOS was poor (quality is already ingested).
- [ ] **Harden ingest** — track/rotate processed CDR files so re-runs don't
      double-count (needed before the scheduled prod ingest).
- [ ] **Vendor HTMX locally** — `base.html` loads it from cdnjs; a locked-down
      VM can't reach that. Drop `htmx.min.js` into `app/static/` before go-live.
- [ ] Verify `config/models.yaml` PoE/EoL against Cisco data sheets and mark
      each `verified` (now doable in the `/catalog` UI).

---

## Gotchas worth remembering

- **AXL only runs on the publisher.** Pointing at a subscriber looks like a
  permissions error. RisPort answers on any node.
- **Never let the app write to CUCM** — read-only is why it's approved against
  production. See CLAUDE.md.
- **Ingest is additive with no de-dup** — ingest each CDR batch once.
- **`make dev-seed` loads 600 mock phones** — don't run it on an instance you're
  pointing at real CUCM.
- Non-Docker `make mock`/`make dev` need the venv active (`source .venv/bin/activate`).
