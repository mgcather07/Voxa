# Voxa — running on the server

This folder is everything the server needs to run Voxa. It contains **no
application source** — the app ships as a pre-built container image. You should
have received two files:

- `voxa-deploy-<version>.tar.gz` — this bundle (compose file, nginx config, env
  template)
- `voxa-image-<version>.tar.gz` — the application image

Prerequisites on the VM: Docker Engine + the Compose plugin, and a route to
CUCM (8443) and the phone subnets (80/443). See the full `docs/DEPLOY.md` for
VM sizing and firewall rules.

---

## 1. Unpack and load the image

```bash
sudo mkdir -p /opt/voxa && sudo chown $USER /opt/voxa
tar xzf voxa-deploy-<version>.tar.gz -C /opt/voxa --strip-components=1
cd /opt/voxa

gunzip -c /path/to/voxa-image-<version>.tar.gz | docker load
docker image ls voxa        # confirm the tag that loaded
```

*(If you use an internal registry instead of a tarball, skip the `docker load`
and just `docker pull` the image, then set `VOXA_IMAGE` to that path below.)*

## 2. Configure

```bash
cp .env.prod.example .env.prod
chmod 600 .env.prod
docker run --rm python:3.12-slim python -c \
  "import secrets; print(secrets.token_urlsafe(48))"   # value for SECRET_KEY
```

Edit `.env.prod` and set at least:

- `VOXA_IMAGE` — the exact tag that loaded in step 1 (e.g. `voxa:1.4.0`)
- `SECRET_KEY`, `POSTGRES_PASSWORD`
- `CUCM_HOST`, `CUCM_USER`, `CUCM_PASSWORD`

## 3. TLS certificate

Put a cert/key from your internal CA at `deploy/certs/server.crt` and
`deploy/certs/server.key` (append the intermediate chain to the crt). For a
first look before a cert is issued, a self-signed pair works:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 397 \
  -keyout deploy/certs/server.key -out deploy/certs/server.crt \
  -subj "/CN=phones.corp.example.com"
chmod 600 deploy/certs/server.key
```

## 4. Start

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d
docker compose --env-file .env.prod -f docker-compose.prod.yml \
  exec app python scripts/manage.py create-user yourname --admin
```

Browse to `https://<vm>/`, sign in, click **Collect from CUCM**. If collection
fails, the connectivity check names the missing CUCM role:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml \
  exec app python scripts/test_cucm.py
```

---

## Updating to a new version

You receive a new `voxa-image-<newversion>.tar.gz`:

```bash
cd /opt/voxa
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T db \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > backup-before-upgrade.sql.gz

gunzip -c /path/to/voxa-image-<newversion>.tar.gz | docker load
# bump VOXA_IMAGE in .env.prod to the new tag, then:
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d
```

Rollback is the same move in reverse: set `VOXA_IMAGE` back to the previous tag
(still loaded on the host) and `up -d` again.

## Backups

Back up the Postgres volume and `.env.prod`. The phone inventory can always be
re-collected from CUCM; the only data that can't be regenerated is the
human-entered swap statuses/notes and the user accounts.
