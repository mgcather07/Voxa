#!/usr/bin/env bash
#
# Voxa guided installer — run once on the server, from the unpacked deploy
# bundle. Idempotent: safe to re-run; it never overwrites secrets you've set.
#
#   ./install.sh
#
# What it does, asking before anything destructive:
#   1. checks Docker + Compose are present
#   2. loads the Voxa image tarball if one is sitting next to this script
#   3. writes .env.prod from the template, generating strong secrets
#   4. drops in a self-signed TLS cert if you don't have a real one yet
#   5. starts the stack
# Then you open the URL it prints and create your admin in the browser.

set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!  \033[0m %s\n' "$*"; }
die()  { printf '\033[31mx  \033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. prerequisites -------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine first."
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
else die "Docker Compose is not available (need 'docker compose' or 'docker-compose')."; fi
docker info >/dev/null 2>&1 || die "The Docker daemon isn't reachable. Start it (and check your user is in the docker group)."
say "Docker OK ($COMPOSE)"

gen_secret() {  # url-safe, no newline
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-48
  else head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

# --- 2. load the image tarball, if shipped ----------------------------------
IMG_TAR=$(ls voxa-image-*.tar.gz 2>/dev/null | head -1 || true)
if [ -n "${IMG_TAR:-}" ]; then
  say "Loading image from $IMG_TAR"
  gunzip -c "$IMG_TAR" | docker load
else
  warn "No image tarball here. The stack will pull VOXA_IMAGE from a registry instead — make sure it's set in .env.prod."
fi

# --- 3. .env.prod -----------------------------------------------------------
if [ -f .env.prod ]; then
  say ".env.prod already exists — leaving it untouched."
else
  say "Creating .env.prod"
  cp .env.prod.example .env.prod
  SECRET=$(gen_secret); PGPW=$(gen_secret)
  # fill blank SECRET_KEY / POSTGRES_PASSWORD in place
  sed -i.bak -E "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|; s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PGPW}|" .env.prod
  rm -f .env.prod.bak
  chmod 600 .env.prod
  # If an image was loaded, pin VOXA_IMAGE to that exact tag (not :latest).
  if [ -n "${IMG_TAR:-}" ]; then
    LOADED=$(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^voxa:[0-9]' | head -1 || true)
    [ -n "$LOADED" ] && sed -i.bak -E "s|^VOXA_IMAGE=.*|VOXA_IMAGE=${LOADED}|" .env.prod && rm -f .env.prod.bak
  fi
  say "Generated SECRET_KEY and POSTGRES_PASSWORD. Review .env.prod for CUCM/app values."
  warn "You can set CUCM connection details here OR in the browser Settings after first login."
fi

# --- 4. TLS certificate -----------------------------------------------------
mkdir -p deploy/certs
if [ -f deploy/certs/server.crt ] && [ -f deploy/certs/server.key ]; then
  say "TLS cert present (deploy/certs/server.crt)."
else
  read -r -p "No TLS cert found. Generate a self-signed one to start? [Y/n] " ans
  if [ "${ans:-Y}" != "n" ] && [ "${ans:-Y}" != "N" ]; then
    read -r -p "  Server hostname (e.g. voxa.corp.example.com) [$(hostname -f 2>/dev/null || hostname)]: " HOST
    HOST=${HOST:-$(hostname -f 2>/dev/null || hostname)}
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
      -keyout deploy/certs/server.key -out deploy/certs/server.crt \
      -subj "/CN=${HOST}" -addext "subjectAltName=DNS:${HOST}" >/dev/null 2>&1
    chmod 600 deploy/certs/server.key
    say "Self-signed cert for ${HOST} written to deploy/certs/."
    warn "Browsers will warn until you replace it with a cert from your internal CA (same filenames)."
  else
    die "Add deploy/certs/server.crt and server.key (from your CA), then re-run."
  fi
fi

# --- 5. start ---------------------------------------------------------------
say "Starting Voxa…"
$COMPOSE --env-file .env.prod -f docker-compose.prod.yml up -d

HOST=$(hostname -f 2>/dev/null || hostname)
cat <<EOF

  Voxa is starting.

  Open   https://${HOST}/   in a browser on the network and the first-run
  setup wizard will walk you through creating your admin account, then
  connecting your CUCM cluster. Nothing is ever written to CUCM.

  Manage it with:   make up | make down | make logs | make backup
  Upgrade later:    see docs/UPGRADE.md

EOF
