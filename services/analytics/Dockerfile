# Build context: repo root (docker-compose sets context: .)
# This allows sharing analytics logic with the anonymizer without duplication.
FROM python:3.12-slim

WORKDIR /code

COPY services/analytics/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt

# Service entry point
COPY services/analytics/src/main.py /code/main.py

# Analytics logic  single source of truth lives in the anonymizer's analytics/
# directory. Copied here at build time so both services always run the same code.
COPY services/anonymizer/src/analytics/dp.py /code/dp.py
COPY services/anonymizer/src/analytics/risk.py /code/risk.py
COPY services/anonymizer/src/analytics/privacy_risk.py /code/privacy_risk.py
COPY services/anonymizer/src/analytics/synthetic_passport.py /code/synthetic_passport.py
COPY services/anonymizer/src/analytics/synthetic.py /code/synthetic.py
COPY services/anonymizer/src/analytics/synthetic_sdv.py /code/synthetic_sdv.py

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 8100
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
