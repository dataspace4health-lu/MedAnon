# Build context must be the repo root.
# docker-compose uses: context: . / dockerfile: services/nlp/Dockerfile
FROM python:3.12-slim

WORKDIR /code

COPY services/nlp/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt

# Download spaCy model (this is the heavy step — ~560 MB)
RUN python -m spacy download en_core_web_lg --quiet

# NLP detector source — shared from the anonymizer
COPY services/anonymizer/src/integrations/nlp/detector.py /code/detector.py
COPY services/anonymizer/src/utils/fhirpath.py /code/utils/fhirpath.py

# Service entry point
COPY services/nlp/src/ /code/

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 8200
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8200"]
