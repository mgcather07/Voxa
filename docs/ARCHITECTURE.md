# Architecture

## Shape

A single FastAPI process serving server-rendered HTML, a Postgres database,
and nginx in front. No JavaScript framework, no build step. HTMX swaps HTML
fragments returned by the same routes that render the pages.

```
                    ┌──────────────┐
   browser ──443──▶ │    nginx     │
                    └──────┬───────┘
                           │ :8000
                    ┌──────▼───────┐        ┌────────────┐
                    │   FastAPI    │───────▶│  Postgres  │
                    └──────┬───────┘        └────────────┘
                           │
          ┌────────────────┼─────────────────┐
          │ :8443          │ :8443           │ :80/:443
     ┌────▼────┐     ┌─────▼─────┐     ┌─────▼─────┐
     │   AXL   │     │ RisPort70 │     │  phones   │
     │  (pub)  │     │ (any node)│     │ (web srv) │
     └─────────┘     └───────────┘     └───────────┘
```

## Why server-rendered

The app's job is to show tables of a few thousand rows and let someone filter
them. A single-page app would add a build step, a dependency tree, and a
second language to a tool that one network engineer maintains. HTMX covers the
interactivity that is actually needed — filtered table refresh, inline status
edit, a polling status banner — in a few attributes.

The practical consequence: any change is a Python file and a Jinja template.
No `npm install` before you can fix a typo.

## Data flow

Collection is a single function, `sync.run_sync()`, run in a background task:

1. **AXL** (`executeSQLQuery`) → every configured phone. Paged with
   `SELECT FIRST n ... WHERE pkid > last ORDER BY pkid`, because the reply is
   size-capped.
2. **RisPort70** (`selectCmDeviceExt`) → live registration state. Paged on the
   `StateInfo` blob, because a reply holds at most 1000 devices.
3. **Phone web servers** → serial and CDP switch/port, scraped concurrently
   from registered phones only, with a low concurrency limit.
4. Merge into one row per device name, enrich from the model catalog, upsert.

Pages read the database only. **No page calls CUCM.** A page that blocks on a
cluster round trip is a page that times out during an outage, which is exactly
when someone needs the inventory.

## Why no zeep

Zeep is the usual Python SOAP client, and the usual choice for AXL. It needs
the AXL WSDL bundle — four files downloaded from the CUCM Plugins page — kept
in sync with the cluster version, and it is slow to build a client from them.

We make two fixed SOAP operations. `app/cucm/soap.py` posts hand-written XML
with `httpx`, strips namespaces from the reply, and turns SOAP faults into a
`CucmError` carrying the fault string. That is roughly eighty lines, has no
version-pinned artifacts to ship, and produces better error messages than a
generic WSDL client.

## The model catalog

`config/models.yaml` maps a phone model to its PoE class, lifecycle state, and
recommended replacement. It is deliberately data, not code, so a network
engineer can correct it without touching Python, and every number the app
reports changes with it.

Lookup normalizes on the bare model number: `Cisco 7962`, `Cisco IP Phone
7962G`, and `CP-7962G` all resolve to `7962`. That keeps the catalog stable
across the different strings AXL, RisPort, and the phones themselves return
for the same device.

Entries carry `verified: false` until a human confirms them against Cisco
documentation, and the dashboard flags unverified models that are actually
present in the cluster. The tool would rather nag than quietly hand someone a
wrong wattage.

## PoE math

Budget figures use the **IEEE class ceiling**, not typical draw:

| Class | Reserved |
|---|---|
| 0 | 12.95 W |
| 1 | 3.84 W |
| 2 | 6.49 W |
| 3 | 12.95 W |
| 4 | 25.50 W (802.3at) |

The class ceiling is what a switch reserves per port, and reserved power is
what runs a closet out of budget — a switch will refuse to power a new device
while showing plenty of headroom in actual draw. Typical draw would produce
prettier numbers and worse decisions.

The limitation worth stating out loud: this only counts phone ports. Access
points, cameras, and everything else share the same budget, and the app cannot
see them. Item 2 in `docs/ROADMAP.md` fixes this by asking the switch.

## Authentication

Routes depend on `auth.require_user`; `auth.get_backend()` picks the
implementation from config. Adding Active Directory means writing one class,
not touching any route. Passwords use stdlib scrypt — no bcrypt, passlib, or
argon2 dependency to audit and patch on a server someone else maintains.

## State and failure

The app holds no state on disk. Everything is in Postgres, so the container is
disposable and a rebuild is a `docker compose up -d --build`.

Failures during a collection are recorded on the `SyncRun` row and shown in
the status banner rather than thrown away in a log. A failed collection leaves
the previous inventory intact — the app degrades to stale data, never to no
data.
