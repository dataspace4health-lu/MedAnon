# Build context: services/trust-gate/
# Stateless pre-privacy trust/quality assessment microservice.
# No FHIR client, no Presidio, no gPAS — talks to the FHIR validator and
# terminology sidecars over HTTP.

FROM python:3.12-slim AS base

RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt

COPY src/ /code/src/
COPY config/ /code/config/

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

WORKDIR /code/src
EXPOSE 8400

ENV PYTHONUNBUFFERED=1 \
    TRUST_GATE_CHECKS_PATH=/code/config/checks.yaml \
    TRUST_GATE_WORKERS=2
# Worker count is env-driven so it can be scaled to match the CPU allocation:
# the assess path is CPU-bound under concurrent load (terminology results are
# cached process-wide), so throughput scales with workers up to the cpu limit.
# Keep workers == allotted cores (set TRUST_GATE_WORKERS + deploy cpus together).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port 8400 --workers ${TRUST_GATE_WORKERS:-2}"]
