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

# Replica counts — read from .env (default to 1 if not set or .env absent).
GPAS_REPLICAS  := $(shell grep -s '^GPAS_REPLICAS=' .env | cut -d= -f2 | tr -d '[:space:]')
NLP_REPLICAS   := $(shell grep -s '^NLP_REPLICAS='  .env | cut -d= -f2 | tr -d '[:space:]')
GPAS_REPLICAS  := $(if $(GPAS_REPLICAS),$(GPAS_REPLICAS),1)
NLP_REPLICAS   := $(if $(NLP_REPLICAS),$(NLP_REPLICAS),1)

# HAPI FHIR image — update both together when bumping the HAPI version.
# v7.x uses Java 17 (eclipse-temurin:17-jre-jammy base).
HAPI_IMAGE     := hapiproject/hapi:v7.6.0
HAPI_JAVA_VER  := 17
HC_DIR      := services/fhir-server/healthcheck

.PHONY: help setup test test-cov lint format batch fetch sync-check \
        up down dev logs build build-ui build-sdv up-sdv build-healthcheck clean \
        init-domains preflight verify _dirs \
        helm-install helm-uninstall helm-lint helm-template helm-build-gpas

# ── Default target ────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "MedAnon — available commands"
	@echo "────────────────────────────────────────────────────────────────"
	@echo "  make setup              Install Python deps into .venv"
	@echo "  make test               Run full test suite"
	@echo "  make test-cov           Run tests with coverage report"
	@echo "  make lint               Run ruff linter"
	@echo "  make format             Auto-format with ruff"
	@echo "  make batch              Run batch processing + analytics"
	@echo "  make fetch              Pull resources from HAPI FHIR, anonymize, write NDJSON"
	@echo ""
	@echo "  make up                 Start full stack (preflight + docker compose + verify) — scales gPAS and NLP from .env"
	@echo "  make dev                Start stack with hot-reload (dev overrides)"
	@echo "  make down               Stop and remove containers"
	@echo "  make build              (Re)build all images"
	@echo "  make build-ui           Build only the React UI image"
	@echo "  make build-sdv          Build anonymizer with SDV synthetic engine (GaussianCopula)"
	@echo "  make up-sdv             Build SDV image and start full stack with SDV engine"
	@echo "  make build-healthcheck  Compile HAPI FHIR health check for correct Java version"
	@echo "  make logs               Tail container logs"
	@echo "  make init-domains       Import SPE domain template into running gPAS (restarts gpas)"
	@echo "  make preflight          Validate prerequisites before starting the stack"
	@echo "  make verify             Smoke-test a running stack (all 5 services)"
	@echo "  make clean              Remove __pycache__ + .pytest_cache"
	@echo ""
	@echo "  make helm-build-gpas    Build custom gPAS Docker image"
	@echo "  make helm-lint          Validate Helm chart (no cluster needed)"
	@echo "  make helm-template      Dry-run: print rendered Kubernetes YAML"
	@echo "  make helm-install       Install / upgrade chart on the active cluster"
	@echo "  make helm-uninstall     Remove the Helm release"
	@echo ""

# ── Python environment ────────────────────────────────────────────────────────
setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r services/anonymizer/requirements.txt
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

sync-check:
	bash scripts/sync_shared_code.sh

# ── Batch processing ──────────────────────────────────────────────────────────
batch:
	bash scripts/batch_process.sh

fetch:
	bash scripts/batch_fetch.sh

# ── Preflight checks ─────────────────────────────────────────────────────────
# Validates prerequisites before starting the stack to catch errors early.
preflight:
	@echo "── Preflight checks ──────────────────────────────────────────"
	@# 1. docker is available
	@command -v docker >/dev/null 2>&1 || { echo "FAIL: docker not found in PATH"; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "FAIL: Docker daemon not running"; exit 1; }
	@echo "  [OK] Docker daemon is running"
	@# 2. docker compose is available
	@docker compose version >/dev/null 2>&1 || { echo "FAIL: docker compose not available"; exit 1; }
	@echo "  [OK] Docker Compose available"
	@# 3. .env file exists
	@test -f .env || { echo "FAIL: .env file not found (copy from .env.example)"; exit 1; }
	@echo "  [OK] .env file present"
	@# 4. Required env vars are set
	@grep -q '^GPAS_DB_PASSWORD=.\+' .env || { echo "FAIL: GPAS_DB_PASSWORD not set in .env"; exit 1; }
	@echo "  [OK] GPAS_DB_PASSWORD is set"
	@grep -q '^HAPI_DB_PASSWORD=.\+' .env || { echo "FAIL: HAPI_DB_PASSWORD not set in .env"; exit 1; }
	@echo "  [OK] HAPI_DB_PASSWORD is set"
	@grep -q '^HAPI_TARGET_DB_PASSWORD=.\+' .env || { echo "FAIL: HAPI_TARGET_DB_PASSWORD not set in .env"; exit 1; }
	@echo "  [OK] HAPI_TARGET_DB_PASSWORD is set"
	@# 5. HAPI FHIR health check class exists and is compiled for correct Java version
	@test -f $(HC_DIR)/HealthCheck.class || { echo "WARN: HealthCheck.class not found — run 'make build-healthcheck'"; exit 1; }
	@python3 -c "\
	with open('$(HC_DIR)/HealthCheck.class','rb') as f: \
	    d=f.read(8); v=int.from_bytes(d[6:8],'big'); \
	    exit(0 if v <= 61 else 1)" 2>/dev/null \
	    || { echo "FAIL: HealthCheck.class compiled for wrong Java version — run 'make build-healthcheck'"; exit 1; }
	@echo "  [OK] HealthCheck.class targets Java <= 17"
	@# 6. Required ports are free
	@for port in $${ANONYMIZER_PORT:-8000} $${HAPI_PORT:-8081} $${HAPI_TARGET_PORT:-8082} $${GPAS_PORT:-8080} $${UI_PORT:-8501}; do \
	    if ss -tlnp 2>/dev/null | grep -q ":$$port " || \
	       lsof -iTCP:$$port -sTCP:LISTEN >/dev/null 2>&1; then \
	        echo "WARN: Port $$port already in use (may conflict)"; \
	    fi; \
	done
	@echo "  [OK] Port check complete"
	@echo "── Preflight passed ──────────────────────────────────────────"
	@echo ""

# ── HAPI FHIR health-check compilation ───────────────────────────────────────
# The HAPI image is distroless (no shell/curl), so we use a tiny Java class
# to probe /actuator/health. It MUST be compiled for the same JDK version
# as the HAPI image. This target auto-detects the version from the image.
build-healthcheck:
	@echo "Compiling HealthCheck.java for Java $(HAPI_JAVA_VER) ($(HAPI_IMAGE))..."
	@docker run --rm \
		-v "$(CURDIR)/$(HC_DIR):/code" \
		-w /code \
		eclipse-temurin:$(HAPI_JAVA_VER)-jdk \
		javac --release $(HAPI_JAVA_VER) HealthCheck.java
	@echo "  HealthCheck.class compiled for Java $(HAPI_JAVA_VER)"

# ── Docker compose helpers ────────────────────────────────────────────────────
build: build-healthcheck
	@# Compose parses command/healthcheck fields (including :? variables) even
	@# during build. Supply a stub so compose can parse the file without a .env.
	@# Shell env vars take precedence over .env in Docker Compose v2.
	MEDANON_REDIS_PASSWORD=$${MEDANON_REDIS_PASSWORD:-build-placeholder} $(COMPOSE) build

build-ui:
	$(COMPOSE) build ui

build-sdv:
	docker build -t medanon-sdv:latest --target sdv services/anonymizer
	@echo "✓ medanon-sdv:latest built — activate with: make up-sdv"

up-sdv: _dirs preflight build-sdv
	ANONYMIZER_IMAGE=medanon-sdv:latest $(COMPOSE) --profile nlp up -d \
		--scale gpas=$(GPAS_REPLICAS) \
		--scale nlp=$(NLP_REPLICAS)
	@echo ""
	@echo "SDV stack running — /generate/synthetic will use GaussianCopula engine"
	@echo "gPAS replicas: $(GPAS_REPLICAS)  |  NLP replicas: $(NLP_REPLICAS)"

_dirs:
	mkdir -p data output

up: _dirs preflight
	$(COMPOSE) --profile nlp up -d \
		--scale gpas=$(GPAS_REPLICAS) \
		--scale nlp=$(NLP_REPLICAS)
	@echo ""
	@echo "gPAS replicas: $(GPAS_REPLICAS)  |  NLP replicas: $(NLP_REPLICAS)"
	@echo "Waiting for services to become healthy..."
	@bash scripts/verify_deployment.sh || true

dev: build-healthcheck
	$(DEV_COMPOSE) up

down:
	$(COMPOSE) --profile analytics --profile nlp --profile ha --profile s3 down --remove-orphans -v

logs:
	$(COMPOSE) logs -f

init-domains:
	bash scripts/init_gpas_domains.sh

# ── Post-startup verification ─────────────────────────────────────────────────
verify:
	@bash scripts/verify_deployment.sh

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
