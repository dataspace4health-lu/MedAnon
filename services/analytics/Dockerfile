# Build context must be the repo root (not services/analytics/).
# docker-compose uses: context: . / dockerfile: services/analytics/Dockerfile
FROM python:3.12-slim

WORKDIR /code

# Install medanon-core shared library (analytics modules + domain types)
COPY packages/medanon-core/ /build/medanon-core/
RUN pip install --no-cache-dir /build/medanon-core/

COPY services/analytics/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt

# Service entry point
COPY services/analytics/src/ /code/

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 8100
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
