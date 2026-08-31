# CLAUDE.md — working notes for Claude Code

Read this before touching anything. It is the handoff for an internal tool
built to run a Cisco phone replacement project.

---

## What this is

A read-only web app that pulls a live phone inventory out of Cisco Unified
Communications Manager and turns it into a refresh plan: what phones exist,
what each maps to, which switch port each one is on, and what the swap does to
the PoE budget per closet.

Server-rendered HTML. FastAPI + Jinja2 + HTMX + Postgres. No JS framework, no
build step, no bundler. If you find yourself adding npm, stop and reconsider.

**It never writes to CUCM.** Every call is a read. Keep it that way — that
property is why the tool was approved to point at production, and losing it
would be expensive to get back.

---

## Run it

```bash
make install                 # venv + dependencies
cp .env.example .env         # then edit
docker compose up -d         # Postgres on host port 5433
make mock                    # 600 fake phones, no CUCM required
make user                    # create an admin login
make dev                     # http://localhost:8000
```

`make check` (`scripts/test_cucm.py`) tests AXL, RisPort, and a phone scrape
independently and names the missing CUCM role when one fails. **Run it before
debugging anything else** — most "the app is broken" reports are a missing
role on the service account.

`AUTH_DISABLED=true` skips login during local development. Never set it on a
deployed instance; the startup log warns loudly if it is on.

---

## Architecture in one pass

```
app/
  cucm/
    soap.py      shared SOAP plumbing, namespace stripping, fault handling
    axl.py       configuration data via executeSQLQuery, paged on pkid
    risport.py   live registration state, paged on StateInfo
    phoneweb.py  serial + CDP switch/port, scraped from phones concurrently
  catalog.py     config/models.yaml -> PoE class, lifecycle, replacement model
  models.py      SQLAlchemy schema
  db.py          engine, session_scope(), get_session() dependency
  auth.py        password hashing, pluggable auth backend, route dependencies
  sync.py        the collection run: three sources merged into one row/phone
  reports.py     every aggregation the pages display
  main.py        FastAPI routes
config/models.yaml   the file humans edit most
deploy/          nginx config, systemd unit, TLS certs (gitignored)
docs/            ARCHITECTURE, CUCM-SETUP, DEPLOY, ROADMAP
scripts/         test_cucm.py, mock_data.py, manage.py
```

Data flows one way: `sync.run_sync()` reads all three sources, merges them,
upserts `phones`, and records a `SyncRun`. Pages read the database only —
**no page ever calls CUCM directly.** Keep it that way; a page that blocks on
a cluster round trip is a page that times out during an outage, exactly when
someone needs the inventory most.

### Where each field comes from

| Field | Source | Notes |
|---|---|---|
| model, description, device pool, configured firmware, DN | AXL `executeSQLQuery` | one SQL query, `SELECT FIRST n` paging on `pkid` |
| registration status, IP, running firmware, CM node | RisPort70 `selectCmDeviceExt` | hard cap 1000 devices/reply, pages on `StateInfo` |
| serial, hardware rev, switch name + port, VLAN | the phone's own web server | needs Web Access enabled; blank when unreachable |
| PoE class, lifecycle, replacement | `config/models.yaml` | human-maintained, see the warning below |

---

## Conventions and hard-won details

**No zeep.** Zeep needs the AXL WSDL bundle downloaded from the CUCM Plugins
page and kept in sync with the cluster version. We make two fixed SOAP
operations; hand-rolled XML over `httpx` avoids shipping version-pinned
artifacts. Don't add it back.

**AXL only runs on the publisher.** Pointing at a subscriber fails in a way
that looks like a permissions error. RisPort answers on any node.

**`executeSQLQuery` caps its reply size.** That is why `axl.iter_phones()`
pages with `SELECT FIRST n ... WHERE pkid > last ORDER BY pkid` rather than
asking for 30,000 rows. Don't "simplify" this.

**RisPort's 1000-device cap is per reply, not per request.** Asking for more
silently returns 1000. Paging is via the `StateInfo` blob echoed back in the
response.

**Phone web tag names vary across firmware generations.** `phoneweb.py`
flattens each XML document to `{lowercase_tag: text}` and tries several
candidate names per field. When a new phone model returns blanks, add its tag
name to the relevant tuple rather than special-casing the model.

**Phone scraping is concurrency-limited on purpose.** 25 simultaneous requests
is polite. Several hundred looks like a port scan to an IPS, and someone will
call you about it.

**PoE math uses the IEEE class ceiling, not typical draw**, because the class
ceiling is what a switch actually reserves per port — the number that runs a
closet out of power. This is deliberate. Don't switch it to typical draw to
make the numbers look better.

**Timezones are UTC everywhere** (`models.utcnow`). Format for display only.

---

## The thing most likely to embarrass someone

`config/models.yaml` ships **working defaults, not verified facts**. Every PoE
class and end-of-life state in it is a reasonable guess that has not been
checked against Cisco's data sheets.

Each entry carries `verified: false`, and the dashboard flags unverified
models that are actually present in the cluster. If you are asked to add or
change a model:

- Do not invent a PoE class or an EoL date. If you cannot cite it, leave the
  entry as `verified: false` and say so in your response.
- Set `verified: true` only when a human has confirmed the entry against the
  Cisco data sheet or EoL bulletin.

The replacement mapping has the same status. Defaults point at the Desk Phone
9800 series, which needs a recent CUCM release and device pack — verify the
cluster supports a model before planning around it.

---

## Authentication

Local accounts today, Active Directory later. The seam is already in place:
routes only ever depend on `require_user`, and `auth.get_backend()` picks the
implementation from `AUTH_BACKEND`.

- `LocalAuthBackend` — accounts in our database, scrypt hashes (stdlib, no
  bcrypt/passlib dependency to audit on a locked-down server).
- `LdapAuthBackend` — stubbed with the intended bind sequence written out in
  its docstring. See `docs/ROADMAP.md` before implementing.

Manage accounts with `scripts/manage.py`. Passwords are prompted for, never
passed as arguments, so they stay out of shell history and the process list.

When LDAP lands: keep `LocalAuthBackend` reachable as a break-glass path for
when a domain controller is unreachable, and never store an AD password —
authentication is a successful re-bind as the user's own DN.

---

## Deployment

A Linux VM on vSphere, Docker Compose stack behind nginx with TLS. See
`docs/DEPLOY.md` for the VM spec, firewall rules, TLS, backup, and upgrade
commands. `deploy/voxa.service` is a systemd fallback if Docker
isn't approved on the server estate.

Secrets live in `.env.prod` on the VM, `chmod 600`, never committed.

---

## Current state (2026-08-31)

The original roadmap is **built** — see [docs/FEATURE-PLAN.md](docs/FEATURE-PLAN.md)
for the full, phased list (all ✅), verified in the local Docker stack. In brief:

- **Fleet:** dashboard, filterable phone table with inline swap tracking,
  **Phone 360** device page, **change history + fleet trend**, CSV import.
- **Telemetry (CDR/CMR):** raw call-record store, **call search**,
  **cradle-to-grave trace with a SIP ladder diagram**, **call analytics**, and
  **concurrency/capacity** (Erlangs, per-trunk utilization).
- **Project:** refresh plan, PoE budget (with SNMP real-draw), **E911 locations**,
  and a **reports engine** (CSV / XLSX / print-to-PDF).
- **System:** a read-only **JSON API** (`/api/v1`, token auth), **opt-in
  webhooks** (off by default), and an **enterprise Settings page**.

**Configuration is now DB-backed, not `.env`.** CUCM clusters and all
operational settings (SNMP, LDAP, CDR dir, scrape tuning, app name) are edited in
**Settings** (`/settings`, admin) via `app/settings_store.py` — DB overrides env.
Only bootstrap values (DATABASE_URL, SECRET_KEY, AUTH_DISABLED) stay in env. The
collector iterates the clusters configured there. Optional deps (`ldap3`,
`pysnmp`) live in `requirements-ldap.txt` / `requirements-snmp.txt`, not the base.

**Current task:** validate a live CUCM connection — see
[docs/CONNECT-LIVE.md](docs/CONNECT-LIVE.md).

Read [docs/ROADMAP.md](docs/ROADMAP.md) only for the original design sketches; the
work it describes is largely done.

There are no tests yet. That was a deliberate deprioritization, not an
oversight — if you add non-trivial parsing logic, a fixture-based test for it
is welcome.

---

## Working agreements

- Ask before adding a dependency. The current list is short on purpose; this
  runs on a server someone else has to patch.
- Keep pages server-rendered. HTMX swaps fragments; that is the whole
  interactivity budget.
- Any change to collection logic must keep `scripts/mock_data.py` working, so
  the app can always be demonstrated without cluster access.
- Log what you did to CUCM at INFO. During an incident, this app's log is
  evidence that it was only reading.
- If a change would make the app write to CUCM, stop and raise it first.
