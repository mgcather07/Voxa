# Deploying on a vSphere VM

The app is a normal Linux web service: a container stack behind nginx. Nothing
about it is vSphere-specific — it needs a VM, a route to CUCM, and a route to
the phone subnets.

**The deployment model:** you build a container image on a machine you control,
and ship only that image to the server. The server never receives the
application source, a git checkout, or a build toolchain — it runs a sealed
artifact. What lives on the server is just: the image, `docker-compose.prod.yml`,
`.env.prod`, `deploy/nginx.conf`, and your TLS certs. `make bundle` packages the
non-image half of that as a single source-free folder (`deploy/SERVER.md` is its
quickstart).

> A note on what this does and doesn't protect: a container image still contains
> readable Python; someone with the image **and** root on the host could extract
> it. The image gives you *operational* separation — the server runs an artifact,
> not your repository — not secrecy from a host administrator. That is the right
> and sufficient property for "don't hand IT my codebase to build and run."

---

## 1. VM specification

Ask for a standard Linux VM. This sizing covers a cluster of ~5,000 phones
with room to spare; a fleet under 1,000 is comfortable on half of it.

| | Requested | Notes |
|---|---|---|
| Guest OS | Ubuntu Server 24.04 LTS or RHEL 9 | whatever your team already patches |
| vCPU | 2 | 4 if you enable CDR ingest later |
| RAM | 4 GB | Postgres plus two app workers |
| Disk | 40 GB thin | ~1 GB used; the rest is headroom for CDR later |
| Network | one vNIC on a server VLAN | see firewall rules below |
| Snapshot | before each upgrade | the database is the only stateful part |

Note for the request: this is an internal, read-only reporting tool. It stores
a Cisco service account credential and phone inventory data, so it should sit
on a server VLAN with restricted inbound access, not a DMZ.

## 2. Firewall rules

Outbound, from the VM:

| Destination | Port | Why |
|---|---|---|
| CUCM publisher | TCP 8443 | AXL and RisPort |
| Phone subnets | TCP 80 and 443 | serial number and CDP switch/port scrape |
| DNS, NTP | usual | |
| OS package repos | 443 | Docker/OS patching only |
| Internal container registry | 443 | only if you pull the image instead of loading a tarball |

Inbound, to the VM:

| Source | Port | Why |
|---|---|---|
| Ops/engineering subnets | TCP 443 | the web UI |
| Ops/engineering subnets | TCP 22 | administration |

Port 80 exists only to redirect to 443.

The phone scrape is what makes reachability interesting: the VM must be able
to reach every phone subnet directly. If it can't, the app still works — serial
numbers and switch ports are simply blank, and the PoE page will be empty.
That is the single most valuable column, so it is worth the firewall
conversation.

---

## 3. Build the image (on your build machine, not the VM)

Do this on a machine with Docker that you control — not the prod server. On
Apple Silicon, `make image` cross-compiles to the server's `linux/amd64` via
buildx, so the image runs on the VM even though you built it on a Mac. (First
cross-build on a Mac needs QEMU; Docker Desktop and Colima ship it. Override the
interpreter/arch with `make image PLATFORM=linux/amd64`.)

```bash
make image                 # builds voxa:<git-version> for linux/amd64
```

Then get it to the VM. Two supported paths — pick one:

**A. Tarball (no registry needed — the robust default for a locked-down VM).**

```bash
make image-save            # writes dist/voxa-image-<version>.tar.gz
make bundle                # writes dist/voxa-deploy-<version>.tar.gz (no source)
# copy both dist/*.tar.gz to the VM over your VPN (scp), then on the VM:
sudo mkdir -p /opt/voxa && sudo chown $USER /opt/voxa
tar xzf voxa-deploy-<version>.tar.gz -C /opt/voxa --strip-components=1
cd /opt/voxa
gunzip -c /path/to/voxa-image-<version>.tar.gz | docker load
```

**B. Internal registry (cleaner once you have one).**

```bash
make image-push REGISTRY=registry.corp.example.com/voice
# on the VM (which needs the deploy bundle from `make bundle`):
docker pull registry.corp.example.com/voice/voxa:<version>
```

Either way, set `VOXA_IMAGE` in `.env.prod` (next step) to the exact tag you
shipped. The VM only needs Docker + the Compose plugin installed:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER && newgrp docker
```

## 4. Configure

```bash
cp .env.prod.example .env.prod
chmod 600 .env.prod
docker run --rm python:3.12-slim python -c \
  "import secrets; print(secrets.token_urlsafe(48))"     # SECRET_KEY
```

Fill in `.env.prod`: `SECRET_KEY`, `POSTGRES_PASSWORD`, `CUCM_HOST`,
`CUCM_USER`, `CUCM_PASSWORD`.

`.env.prod` is gitignored. Keep the real values in whatever your team uses for
secrets; the file on the VM is a copy, not the master.

## 5. TLS certificate

Use your internal CA so AD-joined workstations trust it without a warning.

```bash
mkdir -p deploy/certs
openssl req -new -newkey rsa:2048 -nodes \
  -keyout deploy/certs/server.key \
  -out deploy/certs/server.csr \
  -subj "/CN=phones.corp.example.com"
```

Submit the CSR to your CA. Save the issued certificate as
`deploy/certs/server.crt`, appending the intermediate chain to it. Then:

```bash
chmod 600 deploy/certs/server.key
```

For a first look before a cert is issued, a self-signed pair works and the
browser warning is survivable:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 397 \
  -keyout deploy/certs/server.key -out deploy/certs/server.crt \
  -subj "/CN=phones.corp.example.com"
```

## 6. Start

`make` targets on the server pass `--env-file .env.prod` for you (Compose needs
it for both the `${...}` values and the app's runtime env).

```bash
make up
make create-user NAME=yourname
```

Browse to `https://<vm>/`, sign in, click **Collect from CUCM**.

If the collection fails, run the connectivity check inside the container — it
names the missing CUCM role rather than making you guess:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml \
  exec app python scripts/test_cucm.py
```

---

## Operating it

```bash
make logs        # follow application logs
make backup      # gzipped pg_dump into ./backups
make down        # stop
```

**Upgrade** (you build a new image on your build machine and ship it as in
step 3; the server never rebuilds):

```bash
cd /opt/voxa
make backup
# load the new image tarball (or `docker pull` it from your registry):
make image-load FILE=/path/to/voxa-image-<newversion>.tar.gz
# bump VOXA_IMAGE to the new tag in .env.prod, then:
make up
```

Rollback is the reverse: set `VOXA_IMAGE` back to the previous tag (still loaded
on the host) and `make up`. Take a VM snapshot before an upgrade that changes the
schema. The app creates tables it needs on startup but does not run migrations —
see the note below.

**Restore:**

```bash
make restore FILE=backups/voxa_YYYYMMDD_HHMMSS.sql.gz
```

**Back up:** the Postgres volume and `.env.prod`. Everything else is in git.
The inventory can always be re-collected from CUCM in minutes; the parts that
can't be regenerated are the human-entered swap statuses and notes, and the
user accounts.

---

## Scheduled collection

Voxa has no built-in scheduler on purpose — the OS is the scheduler, which keeps
the dependency list short and makes the timing visible to whoever runs the box.
`scripts/collect.py` runs one collection and exits (non-zero on failure), and
every run records a per-phone snapshot that powers the **History** page (what
appeared, moved, dropped, re-registered) and the fleet trend.

Run one now:

```bash
make collect
```

Schedule it nightly with the bundled systemd timer:

```bash
sudo cp deploy/voxa-collect.service deploy/voxa-collect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voxa-collect.timer
systemctl list-timers voxa-collect.timer
```

Or a host crontab line, if you prefer cron:

```bash
15 2 * * *  cd /opt/voxa && docker compose --env-file .env.prod \
  -f docker-compose.prod.yml exec -T app python scripts/collect.py \
  >> /var/log/voxa-collect.log 2>&1
```

### CDR/CMR call analytics

Point CUCM's Billing Application Server at an SFTP endpoint that lands files in
`CDR_DIR` (default `/var/lib/voxa/cdr`), mount that path into the app container,
and fold new files into per-device call stats on a schedule:

```bash
make ingest        # or: docker compose … exec -T app python scripts/ingest_cdr.py
```

Voxa keeps per-device aggregates (call volume, last call, rough MOS) — enough to
answer "which phones does nobody use, so we needn't replace them" — not raw call
records. The app never opens an SFTP connection itself; landing the files is the
OS's job, same as scheduled collection.

### SNMP PoE polling (optional)

To show each switch's *real* PoE draw and budget next to the class-ceiling
estimate, enable SNMP: add pysnmp to the image (`requirements-snmp.txt`), set
`SNMP_ENABLED=true` and `SNMP_COMMUNITY`, then poll on a schedule:

```bash
make poll        # read-only SNMP against the CDP-discovered switches
```

Polling is read-only (SNMP GET/walk) and targets the switch IPs Voxa already
discovered; the PoE page adds Real draw / Available / Headroom columns once data
is present.

---

## Schema changes

`init_db()` calls `create_all()`, which creates missing tables but **does not
alter existing ones**. That is fine for the current single-VM deployment and
will stop being fine the first time a column changes on a database with real
tracking data in it.

Before the first schema change that matters, add Alembic:

```bash
pip install alembic && alembic init migrations
```

Point `sqlalchemy.url` at the app's `DATABASE_URL`, autogenerate the initial
revision from the current models, and stamp the existing database with it.
Until then, note that dropping and recreating the database loses swap statuses
and notes, not just cached inventory.

---

## Without Docker

If containers aren't approved on your server estate, `deploy/voxa.service`
is a systemd unit. You'll need Python 3.12 and Postgres installed on the VM,
the repo at `/opt/voxa` with a venv, a `voxa` system user, and
nginx configured as a reverse proxy to `127.0.0.1:8000`. Same `.env.prod`.

---

## A note on the OVA question

An **OVA** is a whole-VM image, and it is reasonable to ask why we don't ship
one. An OVA makes sense for handing a black box to strangers who administer
their own estate. Here you administer the VM, and the thing that actually needs
to travel is the *app*, not an operating system — so we ship a **container
image** onto a VM your team already patches and monitors. You still never copy
source to the server (that was the whole point), but the VM's OS lifecycle stays
under your standard build process instead of frozen inside an appliance you now
own. Revisit the OVA only if this ends up deployed somewhere you don't
administer.
