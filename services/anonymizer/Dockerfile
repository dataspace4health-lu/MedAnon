# Build context must be the repo root.
# docker-compose uses: context: . / dockerfile: services/anonymizer/Dockerfile
#
# ── Base stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

# Upgrade Debian packages to pick up OS-level security patches (Trivy HIGH/CRITICAL)
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /code

# Install medanon-core shared library first (layer-cached unless package changes)
COPY packages/medanon-core/ /build/medanon-core/
RUN pip install --no-cache-dir /build/medanon-core/

# Install service deps (layer-cached unless requirements change)
COPY services/anonymizer/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Download spaCy model
RUN python -m spacy download en_core_web_lg --quiet

COPY services/anonymizer/src/ /code/src/
COPY services/anonymizer/config/ /code/config/
# NOTE: RSA keys are NOT baked into the image.
# Mount them at runtime via Docker volume or Kubernetes Secret:
#   docker run -v /path/to/keys:/code/keys ...
#   Kubernetes: secretRef → volumeMount at /code/keys

# Create a non-root user for production security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Pre-create /output with correct ownership so the CLI batch mode can write there
# when the host directory is mounted at runtime.
RUN mkdir -p /output && chown appuser:appgroup /output

EXPOSE 8000

# ── Production stage (default) ────────────────────────────────────────────────
FROM base AS prod
WORKDIR /code/src
USER appuser
CMD gunicorn api.main:app -k uvicorn.workers.UvicornWorker \
    --workers ${MEDANON_WORKERS:-4} --bind 0.0.0.0:8000 \
    --timeout ${MEDANON_REQUEST_TIMEOUT_SEC:-300} --graceful-timeout 30

# ── Development stage — used by docker-compose.dev.yml ───────────────────────
FROM base AS dev
RUN pip install --no-cache-dir watchfiles
WORKDIR /code/src
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ── SDV stage — full synthetic data capability (GaussianCopula engine) ────────
# Extends base before the non-root user switch so pip can write to site-packages.
# Build: make build-sdv   Start: make up-sdv
FROM base AS sdv
COPY services/anonymizer/requirements-sdv.txt /code/requirements-sdv.txt
RUN pip install --no-cache-dir -r /code/requirements-sdv.txt
WORKDIR /code/src
USER appuser
CMD gunicorn api.main:app -k uvicorn.workers.UvicornWorker \
    --workers ${MEDANON_WORKERS:-4} --bind 0.0.0.0:8000 \
    --timeout ${MEDANON_REQUEST_TIMEOUT_SEC:-300} --graceful-timeout 30
