# Build context: repo root (.) - so the shared medanon-core package can be
# copied in. docker-compose uses: context: . / dockerfile: services/anonymizer/Dockerfile
#
# ── Base stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

# 1. Upgrade all packages (fixes CVEs that have patches available in Debian repos)
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /code

# Install service deps (layer-cached unless requirements change)
COPY services/anonymizer/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Shared inner package (domain + analytics + scoring): the single source of
# truth, installed as a wheel into site-packages (importable regardless of cwd).
COPY packages/medanon-core /code/medanon-core
RUN pip install --no-cache-dir /code/medanon-core

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

# ── API stage  public HTTP server (anonymizer) ───────────────────────────────
# Default production target. Runs the FastAPI app via gunicorn + uvicorn workers.
FROM base AS api
WORKDIR /code/src
USER appuser
EXPOSE 8000
CMD gunicorn api.main:app -k uvicorn.workers.UvicornWorker \
    --workers ${MEDANON_WORKERS:-4} --bind 0.0.0.0:8000 \
    --timeout ${MEDANON_REQUEST_TIMEOUT_SEC:-120} --graceful-timeout 120

# ── Worker stage  async job executor (no HTTP server) ────────────────────────
# Same code, no gunicorn/uvicorn process. Listens on Redis/Postgres for jobs and
# exposes Prometheus metrics on :9091. Slimmer attack surface than the API image
# because there is no public web port.
FROM base AS worker
WORKDIR /code/src
USER appuser
EXPOSE 9091
ENTRYPOINT ["python", "-m", "pipeline.jobs.worker_main"]

# ── Production alias  keeps `target: prod` working for legacy compose files ──
FROM api AS prod

# ── Development stage  used by docker-compose.dev.yml ───────────────────────
FROM base AS dev
RUN pip install --no-cache-dir watchfiles
WORKDIR /code/src
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ── SDV stage  full synthetic data capability (GaussianCopula engine) ────────
# Extends base before the non-root user switch so pip can write to site-packages.
# Build: make build-sdv   Start: make up-sdv
FROM base AS sdv
COPY services/anonymizer/requirements-sdv.txt /code/requirements-sdv.txt
RUN pip install --no-cache-dir -r /code/requirements-sdv.txt
WORKDIR /code/src
USER appuser
CMD gunicorn api.main:app -k uvicorn.workers.UvicornWorker \
    --workers ${MEDANON_WORKERS:-8} --bind 0.0.0.0:8000 \
    --timeout ${MEDANON_REQUEST_TIMEOUT_SEC:-120} --graceful-timeout 120
