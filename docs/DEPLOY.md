# Deploying on a vSphere VM

The app is a normal Linux web service: a container stack behind nginx. Nothing
about it is vSphere-specific — it needs a VM, a route to CUCM, and a route to
the phone subnets.

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
| Package repos / registry | 443 | only for build and patching |

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

## 3. Install

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git make
sudo usermod -aG docker $USER && newgrp docker

sudo mkdir -p /opt/voxa && sudo chown $USER /opt/voxa
git clone <your-repo-url> /opt/voxa
cd /opt/voxa
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

```bash
make build
make up
docker compose -f docker-compose.prod.yml exec app \
  python scripts/manage.py create-user yourname --admin
```

Browse to `https://<vm>/`, sign in, click **Collect from CUCM**.

If the collection fails, run the connectivity check inside the container — it
names the missing CUCM role rather than making you guess:

```bash
docker compose -f docker-compose.prod.yml exec app python scripts/test_cucm.py
```

---

## Operating it

```bash
make logs        # follow application logs
make backup      # gzipped pg_dump into ./backups
make down        # stop
```

**Upgrade:**

```bash
cd /opt/voxa
make backup
git pull
make build && make up
```

Take a VM snapshot before an upgrade that changes the schema. The app creates
tables it needs on startup but does not run migrations — see the note below.

**Restore:**

```bash
gunzip -c backups/voxa_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db \
  psql -U voxa voxa
```

**Back up:** the Postgres volume and `.env.prod`. Everything else is in git.
The inventory can always be re-collected from CUCM in minutes; the parts that
can't be regenerated are the human-entered swap statuses and notes, and the
user accounts.

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

Call Telemetry ships as a VMware OVA, and it is reasonable to ask why this
doesn't. An OVA makes sense for distributing to strangers who can't be asked
to run `git clone`. For a tool your own team deploys to your own vSphere, a
git checkout plus `make up` is faster to build, far easier to patch, and
leaves the VM's OS lifecycle under your standard build process instead of
inside an image you now maintain. Revisit only if this ends up deployed
somewhere you don't administer.
