"""SPE FHIR BlackBox — anonymizer service root package.

The anonymizer is a rule-driven FHIR de-identification and pseudonymisation
engine.  Entry points:

- ``api/main.py``       — FastAPI REST service (HTTP interface)
- ``cli/main.py``       — Command-line interface for bulk processing
- ``pipeline/``         — Core de-identification pipeline
- ``integrations/``     — External service adapters (gPAS, NLP, FHIR, Redis, Postgres)
- ``actions/``          — One file per de-identification action (redact, cryptohash, …)
"""
