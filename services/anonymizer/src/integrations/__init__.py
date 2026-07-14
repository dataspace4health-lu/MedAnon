"""integrations  External service adapters.

Sub-packages:
    fhir/        FHIR R4 server client (read, write, bulk)
    gpas/        gPAS pseudonymisation service client
    nlp/         NLP microservice client (PHI detection)
    postgres/    PostgreSQL job/config/subscription stores
    redis/       Redis job store + L2 cache
    staging/     PostgreSQL two-phase staging store
    ai/          LLM provider + AI agent implementations
    analytics/   Analytics microservice proxy
    scoring/     Optional remote scoring microservice client
    storage/     Result storage backend (filesystem or S3)
"""
