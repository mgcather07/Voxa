.PHONY: help install dev mock user check build up down logs backup restore shell

help:
	@echo "Local development"
	@echo "  make install    create .venv and install dependencies"
	@echo "  make dev        run the app with reload on :8000"
	@echo "  make mock       seed 600 fake phones (no CUCM needed)"
	@echo "  make user       create an admin account"
	@echo "  make check      test CUCM connectivity and permissions"
	@echo ""
	@echo "Production (on the VM)"
	@echo "  make build      build the app image"
	@echo "  make up         start the stack"
	@echo "  make down       stop the stack"
	@echo "  make logs       follow app logs"
	@echo "  make backup     dump the database to ./backups"
	@echo "  make shell      psql into the running database"

PROD := docker compose -f docker-compose.prod.yml

install:
	python3 -m venv .venv
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

build:
	$(PROD) build

up:
	$(PROD) up -d
	@echo "Started. Create the first account with:"
	@echo "  $(PROD) exec app python scripts/manage.py create-user admin --admin"

down:
	$(PROD) down

logs:
	$(PROD) logs -f app

backup:
	@mkdir -p backups
	$(PROD) exec -T db pg_dump -U $${POSTGRES_USER:-phoneinv} $${POSTGRES_DB:-phone_inventory} \
		| gzip > backups/phone_inventory_$$(date +%Y%m%d_%H%M%S).sql.gz
	@echo "Wrote backups/phone_inventory_$$(date +%Y%m%d)*.sql.gz"

shell:
	$(PROD) exec db psql -U $${POSTGRES_USER:-phoneinv} $${POSTGRES_DB:-phone_inventory}
