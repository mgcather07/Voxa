.PHONY: help install dev mock user check \
        dev-up dev-seed dev-logs dev-down dev-reset \
        image image-save image-load image-push bundle release \
        up down logs backup restore shell create-user collect ingest poll

# --- Configuration ----------------------------------------------------------
# Interpreter for the local dev venv. The stock macOS `python3` may be too old
# (see docs); override, e.g. make install PYTHON=/opt/homebrew/bin/python3.12
PYTHON   ?= python3

# Image name and version. VERSION defaults to the git description so every
# build is traceable to a commit; override for a release tag: make image VERSION=1.4.0
IMAGE    ?= voxa
VERSION  ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)

# The production server is amd64 (vSphere Linux). Building on Apple Silicon
# cross-compiles to this via buildx/QEMU.
PLATFORM ?= linux/amd64

# Optional internal registry, e.g. REGISTRY=registry.corp.example.com/voice
REGISTRY ?=

DIST     := dist

# Every prod compose command reads .env.prod for BOTH variable interpolation
# (${POSTGRES_*}, ${VOXA_IMAGE}) and the app's runtime env. Keep them in sync.
PROD := docker compose --env-file .env.prod -f docker-compose.prod.yml

help:
	@echo "Local development (needs the venv active: source .venv/bin/activate)"
	@echo "  make install    create .venv and install dependencies"
	@echo "  make dev        run the app with reload on :8000"
	@echo "  make mock       seed 600 fake phones (no CUCM needed)"
	@echo "  make user       create an admin account"
	@echo "  make check      test CUCM connectivity and permissions"
	@echo ""
	@echo "Containerized local dev (Docker on this machine, live reload)"
	@echo "  make dev-up      build + start the dev stack on :8000"
	@echo "  make dev-seed    load 600 mock phones (first run)"
	@echo "  make dev-logs    follow the app container logs"
	@echo "  make dev-down    stop the dev stack"
	@echo "  make dev-reset   stop and wipe the dev database"
	@echo ""
	@echo "Release (tag a version; GitHub Actions builds & publishes it)"
	@echo "  make release VERSION=1.0.0   tag v1.0.0 and push it"
	@echo ""
	@echo "Build & ship the image locally (build machine with Docker + buildx)"
	@echo "  make image        build voxa:\$$(VERSION) for $(PLATFORM)"
	@echo "  make image-save   export it to $(DIST)/ as a .tar.gz for transfer"
	@echo "  make image-push   push to an internal registry (set REGISTRY=)"
	@echo "  make bundle       assemble the source-free server deploy folder"
	@echo ""
	@echo "Run & operate (on the prod server; needs .env.prod present)"
	@echo "  make image-load FILE=voxa-image-X.tar.gz   load a shipped image"
	@echo "  make up           start the stack"
	@echo "  make down         stop the stack"
	@echo "  make logs         follow app logs"
	@echo "  make create-user NAME=alice   create an admin login"
	@echo "  make collect      run one collection now (schedule via cron/timer)"
	@echo "  make ingest       fold CDR/CMR files from CDR_DIR into call stats"
	@echo "  make poll         SNMP-poll switches for real PoE (needs pysnmp)"
	@echo "  make backup       dump the database to ./backups"
	@echo "  make restore FILE=backups/voxa_X.sql.gz   restore a dump"
	@echo "  make shell        psql into the running database"

# --- Local development ------------------------------------------------------
install:
	$(PYTHON) -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements.txt
	@echo "Done. Activate with: source .venv/bin/activate"

dev:
	uvicorn app.main:app --reload --port 8000

mock:
	python scripts/mock_data.py 600 --wipe

user:
	python scripts/manage.py create-user admin --admin

check:
	python scripts/test_cucm.py

# --- Containerized local dev (Docker on this machine) -----------------------
DEV := docker compose -f docker-compose.dev.yml

dev-up:
	$(DEV) up -d --build
	@echo "App starting on http://localhost:8000"
	@echo "First run? Load sample data with: make dev-seed"

dev-seed:
	$(DEV) exec app python scripts/mock_data.py 600 --wipe

dev-logs:
	$(DEV) logs -f app

dev-down:
	$(DEV) down

dev-reset:
	$(DEV) down -v

# --- Release ----------------------------------------------------------------
# Tag a version and push it; the GitHub Actions `release` workflow builds and
# publishes the image + tarballs for that tag. Normalizes 1 -> 1.0.0 so the tag
# matches the workflow's v*.*.* trigger, and refuses to tag a dirty tree so the
# image always corresponds to a real commit.
release:
	@test -n "$(VERSION)" || { echo "Usage: make release VERSION=1.0.0"; exit 1; }
	@V=$$(echo "$(VERSION)" | sed -E 's/^v//'); \
	case "$$V" in \
	  *.*.*) : ;; \
	  *.*)   V="$$V.0" ;; \
	  *)     V="$$V.0.0" ;; \
	esac; \
	echo "$$V" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' \
	  || { echo "Set an explicit version: make release VERSION=1.0.0 (got '$(VERSION)')"; exit 1; }; \
	test -z "$$(git status --porcelain)" \
	  || { echo "Working tree is dirty — commit or stash before releasing:"; \
	       git status --short; exit 1; }; \
	git rev-parse "v$$V" >/dev/null 2>&1 \
	  && { echo "Tag v$$V already exists."; exit 1; } || true; \
	git tag -a "v$$V" -m "Voxa v$$V" && git push origin "v$$V" && \
	echo "Pushed tag v$$V — GitHub Actions will build and publish the release."

# --- Build & ship the image -------------------------------------------------
image:
	docker buildx build --platform $(PLATFORM) \
		--build-arg VOXA_VERSION=$(VERSION) \
		-t $(IMAGE):$(VERSION) -t $(IMAGE):latest --load .
	@echo "Built $(IMAGE):$(VERSION) for $(PLATFORM)"

image-save: image
	@mkdir -p $(DIST)
	docker save $(IMAGE):$(VERSION) | gzip > $(DIST)/voxa-image-$(VERSION).tar.gz
	@echo "Wrote $(DIST)/voxa-image-$(VERSION).tar.gz"
	@echo "Copy it to the server, then: make image-load FILE=voxa-image-$(VERSION).tar.gz"

image-push:
	@test -n "$(REGISTRY)" || (echo "Set REGISTRY=registry.host/path"; exit 1)
	docker tag $(IMAGE):$(VERSION) $(REGISTRY)/$(IMAGE):$(VERSION)
	docker push $(REGISTRY)/$(IMAGE):$(VERSION)
	@echo "Pushed $(REGISTRY)/$(IMAGE):$(VERSION) — set VOXA_IMAGE to it in .env.prod"

# Assemble exactly what the server needs — compose, nginx, env template, docs —
# with NO application source, into a single tarball to hand over alongside the
# image tarball.
bundle:
	@mkdir -p $(DIST)/voxa-deploy-$(VERSION)/deploy/certs
	@mkdir -p $(DIST)/voxa-deploy-$(VERSION)/docs
	cp docker-compose.prod.yml $(DIST)/voxa-deploy-$(VERSION)/
	cp .env.prod.example       $(DIST)/voxa-deploy-$(VERSION)/
	cp Makefile                $(DIST)/voxa-deploy-$(VERSION)/
	cp deploy/nginx.conf       $(DIST)/voxa-deploy-$(VERSION)/deploy/
	cp deploy/install.sh       $(DIST)/voxa-deploy-$(VERSION)/install.sh
	chmod +x                   $(DIST)/voxa-deploy-$(VERSION)/install.sh
	cp deploy/SERVER.md        $(DIST)/voxa-deploy-$(VERSION)/README.md
	cp docs/UPGRADE.md         $(DIST)/voxa-deploy-$(VERSION)/docs/
	cp docs/DEPLOY.md          $(DIST)/voxa-deploy-$(VERSION)/docs/ 2>/dev/null || true
	touch $(DIST)/voxa-deploy-$(VERSION)/deploy/certs/.gitkeep
	cd $(DIST) && tar czf voxa-deploy-$(VERSION).tar.gz voxa-deploy-$(VERSION)
	@echo "Wrote $(DIST)/voxa-deploy-$(VERSION).tar.gz (source-free server bundle)"
	@echo "Ship it with voxa-image-$(VERSION).tar.gz; on the server: tar xzf, ./install.sh"

# --- Run & operate on the server --------------------------------------------
image-load:
	@test -n "$(FILE)" || (echo "Set FILE=path/to/voxa-image-X.tar.gz"; exit 1)
	gunzip -c $(FILE) | docker load

up:
	$(PROD) up -d
	@echo "Started. On first run, open https://<this-host>/ in a browser —"
	@echo "the setup wizard creates your admin account. (CLI alternative:"
	@echo "  make create-user NAME=yourname)"

down:
	$(PROD) down

logs:
	$(PROD) logs -f app

create-user:
	@test -n "$(NAME)" || (echo "Set NAME=login"; exit 1)
	$(PROD) exec app python scripts/manage.py create-user $(NAME) --admin

collect:
	$(PROD) exec -T app python scripts/collect.py

ingest:
	$(PROD) exec -T app python scripts/ingest_cdr.py

poll:
	$(PROD) exec -T app python scripts/poll_switches.py

backup:
	@mkdir -p backups
	$(PROD) exec -T db pg_dump -U $${POSTGRES_USER:-voxa} $${POSTGRES_DB:-voxa} \
		| gzip > backups/voxa_$$(date +%Y%m%d_%H%M%S).sql.gz
	@echo "Wrote backups/voxa_$$(date +%Y%m%d)*.sql.gz"

restore:
	@test -n "$(FILE)" || (echo "Set FILE=backups/voxa_X.sql.gz"; exit 1)
	gunzip -c $(FILE) | $(PROD) exec -T db psql -U $${POSTGRES_USER:-voxa} $${POSTGRES_DB:-voxa}

shell:
	$(PROD) exec db psql -U $${POSTGRES_USER:-voxa} $${POSTGRES_DB:-voxa}
