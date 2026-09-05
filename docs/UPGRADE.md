# Upgrading Voxa

Voxa upgrades are **image swaps**. Your data lives in the Postgres volume, and
the app runs its database migrations automatically on start, so an upgrade is:
load the new image, point at it, restart. Downgrades are the same in reverse,
with one caveat noted below.

Everything here runs on the server, from the deploy bundle directory.

---

## 1. Back up first (always)

```bash
make backup
```

Writes `backups/voxa_YYYYMMDD_HHMMSS.sql.gz` — a full database dump. Takes
seconds. Do this before every upgrade; it's your rollback if anything is wrong.

---

## 2. Get the new image onto the server

**Offline / air-gapped (image tarball):**

```bash
make image-load FILE=voxa-image-1.5.0.tar.gz
```

**From a registry:** nothing to load — you'll pin the tag in the next step and
Compose pulls it.

---

## 3. Point at the new version and restart

Edit `.env.prod` and set the new tag:

```
VOXA_IMAGE=voxa:1.5.0
```

Then:

```bash
make up          # recreates the app on the new image
make logs        # watch it come up
```

On start the app **runs any pending database migrations automatically**
(under a lock, so multiple workers are safe). You'll see Alembic lines in the
log ending at the new revision, then `Database ready`. That's it — refresh the
browser.

Pinning the exact tag (never `:latest`) is what makes an upgrade reproducible
and a rollback a one-line change.

---

## 4. Verify

- The page loads over HTTPS and you can sign in.
- **Settings** shows your cluster still connected (config is in the database,
  untouched by the upgrade).
- The dashboard shows your phone count.

---

## Rolling back

If the new version misbehaves:

```bash
# put the old tag back in .env.prod, then:
make up
```

That restores the previous **image**. The **database** was migrated forward and
Voxa's migrations don't auto-downgrade, so if the release included a schema
change and the old image can't read the new schema, restore the pre-upgrade
dump as well:

```bash
make restore FILE=backups/voxa_YYYYMMDD_HHMMSS.sql.gz
make up
```

This is exactly why step 1 is non-negotiable. Most upgrades are additive and the
old image runs fine against the new schema — but the backup means you never have
to find out the hard way.

---

## Backups on a schedule

`make backup` is a single `pg_dump`; wire it to cron for nightly dumps and copy
them off-box:

```cron
15 2 * * *  cd /opt/voxa && make backup && find backups -name '*.sql.gz' -mtime +30 -delete
```

Keep the backups directory (and `.env.prod`, which holds `SECRET_KEY`) in your
normal server backup rotation. Losing `SECRET_KEY` only signs everyone out;
losing the database loses your inventory and call history.

---

## What an upgrade never touches

- Your **database** (phones, call records, settings, users) — it's a named
  Docker volume, independent of the app image.
- Your **`.env.prod`** and **TLS certs**.
- **CUCM** — Voxa is read-only against it in every version.
