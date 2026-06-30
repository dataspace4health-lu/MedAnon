# Build context: services/nlp/
FROM python:3.12-slim

WORKDIR /code

COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt

# Download spaCy models.  English is always required (~560 MB).
# French (fr_core_news_lg, ~545 MB) is opt-in: set NLP_LANG_FR=true at build time.
ARG NLP_LANG_FR=false
RUN python -m spacy download en_core_web_lg --quiet && \
    if [ "$NLP_LANG_FR" = "true" ]; then python -m spacy download fr_core_news_lg --quiet; fi

# NLP service entry point
COPY src/ /code/

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 8200
# Use multiple workers so the NLP service can handle concurrent requests while
# spaCy/Presidio is processing a batch in one worker.  Each worker loads its own
# copy of the spaCy model (~560 MB), so scale with available RAM.
# NLP_WORKERS default 2 keeps memory overhead modest; increase in docker-compose.yml.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8200 --workers ${NLP_WORKERS:-2}"]
