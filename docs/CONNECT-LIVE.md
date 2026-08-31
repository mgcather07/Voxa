# Connecting Voxa to a live CUCM cluster

> **Purpose:** run Voxa on a machine that has VPN/network access to CUCM and
> validate a live connection to a real cluster (tested target: **CUCM 15**).
> Voxa is **read-only** — every CUCM call is a read; it never writes.
>
> **It runs entirely in Docker.** `make dev-up` starts two containers via Docker
> Compose — Postgres and the Voxa app — so the only thing you install on the
> MacBook Pro is Docker itself (no Python, no venv). This is the containerized
> **dev stack**, the fastest path for a first live test. For the hardened
> production deployment on the vSphere VM (also Docker), see [DEPLOY.md](DEPLOY.md).

This runbook is written so either a person or a Claude Code session on the
target machine can follow it. If you are Claude, also read [../CLAUDE.md](../CLAUDE.md)
and [FEATURE-PLAN.md](FEATURE-PLAN.md) first, and see "Notes for Claude" at the end.

---

## What you need first

- **Docker running** (Docker Desktop or Colima). Verify: `docker version`.
- **git**.
- **VPN / network access to CUCM** from this machine (publisher reachable on TCP 8443).
- A **CUCM Application User** (the AXL account) with these roles, pointed at the
  **publisher**:
  - `Standard AXL API Access`
  - `Standard CCM Admin Users (Read Only)`
  - `Standard Serviceability (Read Only)`
  - `Standard RealtimeAndTraceCollection` (needed on some releases for RisPort)
- The **publisher host/IP** and the **AXL version** — for **CUCM 15 use `15.0`**
  (`12.5` also works; AXL is backward-compatible).

CUCM connections are configured **in the app's Settings page**, not in `.env` —
nothing is hard-coded.

---

## Steps

### 1. Get the code — do this BEFORE connecting the VPN

(so `pip` during the image build isn't blocked by a corporate proxy)

```bash
cd ~
git clone https://github.com/mgcather07/Voxa.git   # or the SSH URL
cd Voxa
```

> **Colima users:** clone into your **home folder** (Colima only mounts `$HOME`
> by default). If the repo lives elsewhere, start Colima with that path mounted:
> `colima start --mount /path/to/parent:w` — otherwise the container sees an
> empty source tree.

### 2. Build and start the stack (still off-VPN)

```bash
make dev-up
```

Builds the image (~2–3 min first time) and starts Postgres + the app on
**http://localhost:8000**.

**Do NOT run `make dev-seed`** — that loads 600 mock phones. We want your real
inventory, so leave the database empty.

### 3. Connect the VPN, then prove the container can reach CUCM

This is the #1 snag with Docker on a Mac behind a corporate VPN: the *host* has
the VPN route, but the container's traffic may not follow it. Verify directly
(replace `YOUR_CUCM_PUB`):

```bash
docker compose -f docker-compose.dev.yml exec app \
  python -c "import socket; socket.create_connection(('YOUR_CUCM_PUB',8443),5); print('reachable OK')"
```

- **`reachable OK`** → network path is good; continue.
- **hangs / `timed out` / `No route to host`** → Docker isn't routing to the VPN.
  Fixes: Docker Desktop usually works out of the box; for a split-tunnel VPN, add
  the CUCM subnet to the tunnel, or run the container with host networking. Do not
  proceed to AXL until this returns `reachable OK`.

### 4. Add your cluster in the UI

Open **http://localhost:8000 → Settings → Add a cluster**:

| Field | Value |
|---|---|
| name | e.g. `cucm-prod` |
| publisher host/IP | your publisher |
| service account | your AXL user |
| password | your AXL user's password (you type it) |
| AXL version | **`15.0`** |
| TLS | **off** (CUCM's cert is self-signed) |
| Scrape | off is fine for the first test |

It **auto-tests on save** and shows:

- a green **Connected** or red **Error** badge,
- a **Connection logs** panel with the per-check detail (AXL / Cluster nodes /
  RisPort) — read this first if anything fails,
- the **discovered cluster nodes** (publisher + subscribers). You add the
  publisher only; one connection covers the whole cluster.

### 5. If it's green — pull real data

Click **Collect from CUCM** (top-right). Watch the status line under the title.
This is the first real run of the full AXL → RisPort → phone-scrape pipeline; on
a large cluster it can take a few minutes.

---

## Troubleshooting — read the Connection logs panel first

| Symptom in the logs | Likely cause / fix |
|---|---|
| Reachability check hangs | VPN not routing to the container (see step 3) |
| `AXL: FAIL … 401/403` | Wrong password, or missing `Standard AXL API Access` |
| `AXL: FAIL … not on publisher` style | Point the host at the **publisher** (AXL only runs there) |
| `AXL: FAIL … 599/soap fault about version` | Try AXL version `15.0`, then `12.5` |
| `AXL: FAIL … certificate` | Leave **TLS off** (verify disabled) for a self-signed cert |
| AXL ok but `RisPort: FAIL` | Missing `Standard CCM Admin Users (Read Only)` / `Standard Serviceability (Read Only)` (and `RealtimeAndTraceCollection` on some releases) |
| `Phone web … did not respond` | Expected if Web Access is off or the subnet is unreachable — serial/switch-port columns just stay blank |

`make check` runs the same probe from the CLI (prints each check) if you prefer
the terminal:

```bash
docker compose -f docker-compose.dev.yml exec app python scripts/test_cucm.py
```

---

## Data notes

- The dev database is local to this machine and separate from the Mac Mini.
- Start clean any time: `make dev-reset` (wipes the DB volume) then `make dev-up`.
- A real `Collect from CUCM` upserts real phones. Don't mix real and mock — just
  never run `make dev-seed` on this instance.
- This is a **local test instance** (auth disabled, only reachable on this
  machine). Fine for a connection test; harden it before any real deployment
  (see DEPLOY.md — enable auth, TLS, real hostnames).

---

## When it works → production

Once a live collection succeeds here, the production path is unchanged from
[DEPLOY.md](DEPLOY.md): build/cut a release image (`make release VERSION=…`),
ship it to the vSphere VM, and connect the same cluster there via Settings.

---

## Notes for a Claude Code session on this machine

- The connection **Test** writes only a `ClusterTestLog` row — **no inventory is
  written** — so it is safe to run repeatedly while debugging.
- **Never type the user's CUCM password yourself.** They enter it in the Settings
  form; you help read the **Connection logs** panel and adjust host / AXL version
  / roles based on what it reports.
- `MOCK_MODE` does not gate anything — real AXL/RisPort calls run regardless.
- The collector (`app/sync.py`) iterates the clusters configured in Settings
  (`settings_store.clusters()`), falling back to `.env` `CUCM_*` if none are set.
- If a probe hangs, it is bounded to an 8s timeout per check (`app/cucm_probe.py`).
