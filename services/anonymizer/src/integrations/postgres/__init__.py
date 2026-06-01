"""integrations.postgres — PostgreSQL-backed stores.

Modules:
    pool.py                  — Shared ``ThreadedConnectionPool`` with health checks
    job_store.py             — ``PostgresJobStore``: LISTEN/NOTIFY event-driven job queue
    config_store.py          — Config profile CRUD (``medanon.configs``)
    processing_run_store.py  — Processing history + scoring (``medanon.processing_runs``)
    subscription_store.py    — FHIR Subscription persistence
"""
