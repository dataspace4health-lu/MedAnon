# Build context: repo root (docker-compose sets context: .)
# The analytics logic is the shared medanon-core package (single source of
# truth), installed as a wheel - replacing the old build-time COPY of the
# anonymizer's analytics/*.py files.
FROM python:3.12-slim

WORKDIR /code

COPY services/analytics/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt

# Shared inner package (domain + analytics + scoring). main.py imports
# analytics.* from it (installed into site-packages, importable from any cwd).
COPY packages/medanon-core /code/medanon-core
RUN pip install --no-cache-dir /code/medanon-core

# Service entry point
COPY services/analytics/src/main.py /code/main.py

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 8100
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
