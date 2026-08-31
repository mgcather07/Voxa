# Voxa — Call Telemetry & Phone Refresh Planner

> 📌 **Picking this up on another machine? Start with [HANDOFF.md](HANDOFF.md)** —
> current status, exact setup steps, and what's next.

**Voxa** is a read-only web app that pulls a live phone inventory out of Cisco Unified
Communications Manager and turns it into a refresh plan: what you have, what
it maps to, where each phone is physically plugged in, and what the swap does
to your PoE budget closet by closet.

Nothing in this app writes to CUCM. Every call is a read.

---

## What it collects, and from where

| Data | Source | Notes |
|---|---|---|
| Model, description, device pool, configured firmware, primary DN | **AXL** `executeSQLQuery` over HTTPS 8443 | One SQL query against the CUCM database, paged 1000 rows at a time |
| Registration status, current IP, running firmware, which node | **RisPort70** `selectCmDeviceExt` | Capped at 1000 devices per reply, so it pages on `StateInfo` |
| Serial number, hardware revision, **switch name + port**, VLAN | **The phone's own web server** | CUCM does not reliably hold these. The phone reports its CDP neighbour, which is the access switch and port |
| PoE class, lifecycle state, replacement model | `config/models.yaml` | Yours to edit — see the warning below |

The switch/port data is the part that makes this worth building. Once you know
which switch and port every phone is on, you know which closets are affected,
and you can budget PoE before ordering anything instead of after.

---

## Before you trust the numbers

`config/models.yaml` ships with **working defaults, not verified facts**. The
PoE classes and end-of-life states are reasonable, but you should confirm each
model you actually have against its Cisco data sheet and EoL bulletin before
handing a power budget or a purchase order to anyone. Every entry has a
`verified: false` flag; flip it to `true` as you check each one, and the
dashboard will stop flagging it.

The same goes for the replacement mapping. The defaults point at the Desk
Phone 9800 series, which needs a recent CUCM release and device pack — confirm
your cluster version supports the model before you plan around it.

---

## Setup

### 1. CUCM service account

In **User Management → Application User**, create a user (e.g. `voxa-ro`)
and give it these roles:

- `Standard AXL API Access` — the SQL query
- `Standard CCM Admin Users (Read Only)` — RisPort
- `Standard Serviceability (Read Only)` — RisPort
- `Standard RealtimeAndTraceCollection` — RisPort on some releases

Point the app at the **publisher**. AXL only runs there.

For serial numbers and switch ports you also need **Web Access = Enabled** on
the phones, set either per-phone or (much better) on the Common Phone Profile.
Without it the app still works — those two columns are just blank.

Full detail in [docs/CUCM-SETUP.md](docs/CUCM-SETUP.md).

### 2. Install and run

```bash
make install                 # venv + dependencies
cp .env.example .env         # then edit
docker compose up -d         # Postgres on host port 5433
make check                   # verify CUCM access before anything else
make user                    # create an admin login
make dev                     # http://localhost:8000
```

`make check` (`scripts/test_cucm.py`) tests AXL, RisPort, and a phone scrape
independently and names the missing CUCM role when one fails. Run it before
debugging anything else.

Point `DATABASE_URL` at any Postgres you already have if you'd rather. SQLite
works for a quick look: `DATABASE_URL=sqlite:///./local.db`.

### Want to see it before you have CUCM access?

```bash
make mock                    # 600 realistic phones across 5 sites
```

### Deploying it

A Linux VM on vSphere, Docker Compose behind nginx with TLS. VM sizing,
firewall rules, certificates, backup and upgrade commands are in
[docs/DEPLOY.md](docs/DEPLOY.md). There's a systemd unit in `deploy/` for a VM
where Docker isn't approved.

### Accounts

Local accounts today, Active Directory later — the auth backend is pluggable
and the LDAP bind sequence is sketched out in `app/auth.py`.

```bash
python scripts/manage.py create-user alice --admin
python scripts/manage.py list-users
python scripts/manage.py reset-password alice
```

Passwords are prompted for, never passed as arguments, so they stay out of
shell history. `AUTH_DISABLED=true` skips login for local development only.

---

## The pages

- **Dashboard** — fleet size, how much is past end of support, registration
  health, and how complete your serial/switch-port coverage is.
- **Phones** — the full table, filterable by site, model, lifecycle,
  registration state, and swap status. Search hits name, description, DN, IP,
  serial, and switch. Set each phone's swap status inline as the project moves.
- **Refresh plan** — quantities per replacement model, and the same split by
  site so you can phase the rollout or split the quote.
- **PoE budget** — per switch: how many phone ports, what they reserve today,
  what they will reserve after the swap, and the delta.

**Export CSV** dumps everything, which is what you will actually paste into a
budget request.

---

## A note on the PoE math

Budget numbers use the **IEEE class ceiling**, not typical draw, because the
class ceiling is what a switch reserves per port and therefore what actually
runs a closet out of power:

| Class | Reserved |
|---|---|
| 0 | 12.95 W |
| 1 | 3.84 W |
| 2 | 6.49 W |
| 3 | 12.95 W |
| 4 | 25.50 W (802.3at PoE+) |

Expect the total to go **up**, often sharply. A 7962 is Class 2; most current
replacements are Class 3 or 4. Doubling the reserved wattage on a closet full
of phones is a normal result and a very good thing to discover in a browser
rather than in the field.

This only counts phone ports. Access points, cameras, and everything else on
the same switch draw from the same budget, so compare the number against the
switch's real available power.

---

## Layout

```
app/
  cucm/
    soap.py       shared SOAP plumbing (no WSDL bundle needed)
    axl.py        configuration data via executeSQLQuery
    risport.py    live registration state
    phoneweb.py   serial + CDP switch/port, scraped concurrently
  catalog.py      models.yaml -> PoE, lifecycle, replacement
  models.py       database schema
  sync.py         the collection run
  reports.py      dashboard / plan / PoE aggregations
  auth.py         password hashing, pluggable backend, route guards
  main.py         FastAPI routes
config/models.yaml   the file you will edit most
deploy/           nginx config, systemd unit, TLS certs (gitignored)
docs/             ARCHITECTURE, CUCM-SETUP, DEPLOY, ROADMAP,
                  CONNECT-LIVE (point Voxa at a real cluster), FEATURE-PLAN
scripts/
  test_cucm.py    connectivity and permissions check
  mock_data.py    seed data, no CUCM required
  manage.py       user accounts, secret key generation
```

`CLAUDE.md` is the handoff document — read it before making changes, and point
Claude Code at it.

There is deliberately no `zeep` dependency. Zeep needs the AXL WSDL bundle
downloaded from the CUCM Plugins page and kept in sync with the cluster
version; hand-rolled SOAP over `httpx` avoids shipping version-pinned
artifacts for what amounts to two fixed operations.

---

## Where this goes next, roughly in order of payoff

1. **Scheduled collection** — a cron or APScheduler job nightly, so the data is
   never stale when someone asks.
2. **Change history** — keep a row per sync per phone and you get "what moved,
   what dropped off, what appeared" for free. This is the feature that turns an
   inventory into an operations tool.
3. **Switch-side polling** — SNMP against the access switches gives real PoE
   draw and actual available budget per switch, instead of class estimates.
4. **CDR/CMR ingest** — configure CUCM's Billing Application Server to push CSV
   files to an SFTP endpoint here. That unlocks call volume per device (which
   phones are actually used, so you know what not to replace) and call quality.
5. **E911 / location** — once switch and port are known per phone, mapping them
   to dispatchable locations is mostly a data-entry problem you have already
   solved the hard half of.
