# Build context: repo root (.) - so the shared medanon-core package can be
# copied in. Stateless scoring microservice: it runs the shared scoring engine
# (medanon-core `scoring`); no FHIR client, no Presidio, no NLP (the NLP adapter
# and remote client providers are never injected here, so scoring is local).

FROM python:3.12-slim AS base

RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY services/scoring/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt

# Shared inner package (domain + analytics + scoring): the single source of
# truth, installed as a wheel into site-packages (importable regardless of cwd).
COPY packages/medanon-core /code/medanon-core
RUN pip install --no-cache-dir /code/medanon-core

COPY services/scoring/src/ /code/src/

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

WORKDIR /code/src
EXPOSE 8300

ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8300", "--workers", "2"]
