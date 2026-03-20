# ─────────────────────────────────────────────────────────────────────────────
# MedAnon — developer convenience Makefile
# ─────────────────────────────────────────────────────────────────────────────

VENV        := .venv
PY          := $(VENV)/bin/python3
PIP         := $(VENV)/bin/pip
ANONYMIZER  := services/anonymizer
TEST_DIR    := $(ANONYMIZER)/tests
COMPOSE     := docker compose
DEV_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: help setup test test-cov lint format batch fetch \
        up down dev logs build build-ui clean init-domains _dirs \
        helm-install helm-uninstall helm-lint helm-template helm-build-gpas

# ── Default target ────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "MedAnon — available commands"
	@echo "────────────────────────────────────────────────────────────────"
	@echo "  make setup        Install Python deps into .venv"
	@echo "  make test         Run full test suite"
	@echo "  make test-cov     Run tests with coverage report"
	@echo "  make lint         Run ruff linter"
	@echo "  make format       Auto-format with ruff"
	@echo "  make batch        Run batch processing + analytics"
	@echo "  make fetch        Pull resources from HAPI FHIR, anonymize, write NDJSON"
	@echo ""
	@echo "  make up           Start full stack (docker compose)"
	@echo "  make dev          Start stack with hot-reload (dev overrides)"
	@echo "  make down         Stop and remove containers"
	@echo "  make build        (Re)build all images"
	@echo "  make build-ui     Build only the Streamlit UI image"
	@echo "  make logs         Tail container logs"
	@echo "  make init-domains Import SPE domain template into running gPAS (restarts gpas)"
	@echo "  make clean        Remove __pycache__ + .pytest_cache"
	@echo ""
	@echo "  make helm-build-gpas  Build custom gPAS Docker image"
	@echo "  make helm-lint        Validate Helm chart (no cluster needed)"
	@echo "  make helm-template    Dry-run: print rendered Kubernetes YAML"
	@echo "  make helm-install     Install / upgrade chart on the active cluster"
	@echo "  make helm-uninstall   Remove the Helm release"
	@echo ""

# ── Python environment ────────────────────────────────────────────────────────
setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r services/anonymizer/requirements.txt
	$(PY) -m spacy download en_core_web_lg
	@echo "✓ virtualenv ready — activate with: source $(VENV)/bin/activate"

# ── Testing ───────────────────────────────────────────────────────────────────
test:
	cd $(ANONYMIZER) && $(CURDIR)/$(PY) -m pytest tests/ -q

test-cov:
	cd $(ANONYMIZER) && $(CURDIR)/$(PY) -m pytest tests/ --cov=src --cov-report=term-missing -q

# ── Code quality ──────────────────────────────────────────────────────────────
lint:
	$(VENV)/bin/ruff check services/anonymizer/src

format:
	$(VENV)/bin/ruff format services/anonymizer/src

# ── Batch processing ──────────────────────────────────────────────────────────
batch:
	bash scripts/batch_process.sh

fetch:
	bash scripts/batch_fetch.sh

# ── Docker compose helpers ────────────────────────────────────────────────────
build:
	$(COMPOSE) build

build-ui:
	$(COMPOSE) build ui

_dirs:
	mkdir -p data output

up: _dirs
	$(COMPOSE) up -d

dev:
	$(DEV_COMPOSE) up

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

init-domains:
	bash scripts/init_gpas_domains.sh

# ── Helm (Kubernetes deployment) ─────────────────────────────────────────────
# Build the custom gPAS image (bundles WAR/EAR deployments + CLI scripts).
# Push this image to your registry before running helm-install.
helm-build-gpas:
	docker build -t $(if $(REGISTRY),$(REGISTRY)/,)medanon-gpas:latest services/gpas/

# Validate the chart (no cluster required)
helm-lint:
	helm lint ./helm/medanon

# Dry-run — print all rendered Kubernetes YAML without installing
helm-template:
	helm template medanon ./helm/medanon

# Install or upgrade the release.
# Set REGISTRY, GPAS_URL, and FHIR_SOURCE_URL for your environment, e.g.:
#   make helm-install REGISTRY=ghcr.io/your-org \
#     GPAS_URL=http://medanon-gpas:8080/ttp-fhir/fhir/gpas \
#     FHIR_SOURCE_URL=http://medanon-fhir-server:8080/fhir
helm-install:
	helm upgrade --install medanon ./helm/medanon \
		$(if $(REGISTRY),--set registry=$(REGISTRY),) \
		$(if $(GPAS_URL),--set anonymizer.env.GPAS_URL=$(GPAS_URL),) \
		$(if $(FHIR_SOURCE_URL),--set anonymizer.env.FHIR_SOURCE_URL=$(FHIR_SOURCE_URL),)

# Remove the release (keeps PVCs by default — use kubectl delete pvc to wipe data)
helm-uninstall:
	helm uninstall medanon

# ── Housekeeping ──────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ caches cleared"
