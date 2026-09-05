# Voxa Administrator Guide

Voxa gives you a live inventory of your Cisco phones, a refresh plan with PoE
budgeting, and call telemetry (CDR/CMR) — all **read-only** against Call
Manager. It never writes to CUCM.

This guide is the operator's path from a fresh install to daily use. For
installing the server, see **README.md** / `docs/DEPLOY.md`; for upgrades, see
`docs/UPGRADE.md`.

---

## 1. First login

Open `https://<your-voxa-host>/` in a browser on the network. The first visit
shows a **setup wizard** — create your administrator account there. That account
is a local *break-glass* admin: it keeps working even if you later switch
sign-in to Active Directory and the directory is unreachable.

After setup you land on **Settings**, where you connect your cluster.

---

## 2. Connect Call Manager

You need a **read-only CUCM service account** first — see `docs/CUCM-SETUP.md`
for the exact roles. In short: an Application User with *Standard AXL API
Access* plus the read-only serviceability groups, pointed at the **publisher**
(AXL runs only there).

In **Settings → CUCM clusters → Add a cluster**:

- **Name** — anything (e.g. `hq`)
- **Publisher host / IP** — the CUCM publisher
- **Service account** and **password**
- **AXL version** — match your CUCM (e.g. `15.0` for CUCM 15)
- **TLS** — on if the publisher presents a trusted cert; off for self-signed

Click **Save & retest**. A green *Connected* badge with the discovered cluster
nodes means you're good. If it's red, the **Connection logs** underneath name
the failing piece (almost always a missing role on the service account).

Then run a collection: the **Collect from CUCM** button on the dashboard (or
`make collect` on the server, or schedule it). It reads configuration (AXL),
live registration (RisPort), and each phone's own web page (serial + switch
port). Nothing is written back.

---

## 3. Read the dashboard

- **Inventory tiles** — total phones, past-end-of-support, end-of-sale,
  registered, and how much serial / switch-port data was collected.
- **Cluster nodes** — where registrations actually live across your
  publisher/subscribers.
- **Models in service** — each model, its lifecycle, and the recommended
  replacement. A `°` marks a catalog entry that hasn't been fact-checked against
  Cisco's data sheet; verify it on the **Model catalog** page before ordering.
- **Phones** — the full filterable inventory; click any phone for its detail.

> **PoE figures use IEEE class ceilings** — what a switch *reserves* per port,
> the number that runs a closet out of power. Verify each model against its
> Cisco data sheet before a power budget leaves the building.

---

## 4. Call telemetry (CDR/CMR)

Voxa reads the CDR/CMR files CUCM's **Billing Application Server** produces —
call detail, and MOS/jitter/latency/codec quality. It does **not** poll CUCM
for these; the files are delivered to it.

1. **On CUCM:** enable CDR (Enterprise Parameters → *CDR Enabled Flag*), and add
   a Billing Application Server that pushes the files by SFTP.
2. **In Voxa (Settings → CDR SFTP):** enter that SFTP server's host, user,
   password, and directory. Click **Test connection**, then **Pull & ingest
   now**. Set **Enabled** and an **auto-pull interval** and Voxa fetches new
   files on that schedule automatically — no cron.

Then **Telemetry → Calls / Analytics / Capacity** fill in: call search,
cradle-to-grave traces with a SIP ladder, MOS quality scoring, and trunk
utilization.

---

## 5. Sign-in with Active Directory (optional)

To let engineers log in with their AD accounts:

1. **Settings → LDAP / Active Directory** — set the LDAP URL, a read-only bind
   (service) account, the user base DN, and optionally a required group.
2. Use **Test Active Directory** to verify each step (service bind, user
   lookup, group check, and — with a test password — the actual credential
   bind).
3. Set the **auth backend** to `ldap` and Save.

Local accounts always remain as a **break-glass** path: if every domain
controller is down, your local admin still signs in. AD users are matched
first-name/last-name from the directory; deactivating one in Voxa keeps them
out regardless of AD.

---

## 6. Certificates

**Certificates** reads the TLS certs your CUCM nodes and any CUBEs / border
elements serve — subject, issuer, expiry, self-signed vs CA — via a plain TLS
handshake (still read-only, no CUCM API). Add CUBEs on that page; expiring certs
are flagged.

---

## 7. Housekeeping

- **Backups:** `make backup` on the server dumps the database; schedule it
  nightly and copy the dumps off-box. This is your safety net for upgrades.
- **Retention:** ingested CDR files are archived under `cdr-inbox/processed/`;
  set **Keep processed files (days)** in Settings so disk stays bounded.
- **Users:** manage local accounts in the database via `scripts/manage.py`, or
  rely on AD. Deactivate rather than delete to keep the audit trail.
- **Health:** run `make check` on the server to test AXL, RisPort, and a phone
  scrape independently — the fastest way to see what broke.

---

## The one guarantee

Every CUCM interaction Voxa makes is a read. That is why it is safe to point at
a production cluster, and the application log at INFO is the evidence of it. If a
change would ever make Voxa write to CUCM, that is a defect — report it.
