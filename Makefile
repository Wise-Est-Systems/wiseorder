.PHONY: help bootstrap services-up services-down probe-services migrate test test-pure lint format run ci clean

# Prefer the repo's .venv when present; fall back to system python3.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PIP ?= $(if $(wildcard .venv/bin/pip),.venv/bin/pip,pip3)

help:
	@echo "WiseOrder Runtime — operator commands"
	@echo
	@echo "  make bootstrap        create .venv and install package + dev deps"
	@echo "  make services-up      docker compose up -d (Postgres + Redis)"
	@echo "  make services-down    docker compose down (keep data volumes)"
	@echo "  make probe-services   check DB + Redis reachable; exit non-zero if not"
	@echo "  make migrate          alembic upgrade head"
	@echo "  make test             full pytest run (some tests auto-skip without services)"
	@echo "  make test-pure        smoke + hardening tests only (no services required)"
	@echo "  make lint             ruff check + ruff format --check"
	@echo "  make format           ruff format (write changes — not in CI)"
	@echo "  make run              start the orchestrator"
	@echo "  make ci               lint + test-pure + probe; the CI pre-flight"
	@echo "  make clean            remove .venv, caches, build artifacts"

bootstrap:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"
	@echo "OK: .venv ready. Activate with: source .venv/bin/activate"

services-up:
	docker compose up -d
	@echo "Waiting for services to be healthy..."
	@for i in $$(seq 1 30); do \
	  if docker compose ps --status running 2>/dev/null | grep -q wiseorder-postgres && \
	     docker compose ps --status running 2>/dev/null | grep -q wiseorder-redis; then \
	    echo "OK: services running"; exit 0; \
	  fi; sleep 1; \
	done; \
	echo "WARN: services did not all reach running within 30s"; exit 1

services-down:
	docker compose down

probe-services:
	$(PYTHON) -m core.orchestrator.main --probe-services

migrate:
	$(PYTHON) -m alembic upgrade head

test:
	$(PYTHON) -m pytest tests/ -v --tb=short

test-pure:
	$(PYTHON) -m pytest tests/test_smoke.py tests/test_hardening_v2.py tests/test_distribution_smoke.py -v --tb=short -m "not services_required"

distribute-bootstrap:
	.venv/bin/python -m pip install playwright pytest-asyncio
	.venv/bin/python -m playwright install chromium

distribute-test:
	$(PYTHON) -m pytest tests/test_distribution_smoke.py -v --tb=short -m "not services_required"

lint:
	$(PYTHON) -m ruff check core/ agents/ workflows/ api/ configs/ tests/
	$(PYTHON) -m ruff format --check core/ agents/ workflows/ api/ configs/

format:
	$(PYTHON) -m ruff check --fix core/ agents/ workflows/ api/ configs/ tests/
	$(PYTHON) -m ruff format core/ agents/ workflows/ api/ configs/

run:
	$(PYTHON) -m core.orchestrator.main

ci: lint test-pure
	@echo "OK: CI pre-flight passed"

clean:
	rm -rf .venv .pytest_cache wiseorder.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
