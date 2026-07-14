"""HTTP surface for the Trust Gate service.

Split from the former monolithic main.py:
  - metrics   Prometheus counters/histograms shared by the routers.
  - config    the check config loaded once at import (rules, thresholds, policy).
  - schemas   request/response Pydantic models.
  - deps      store/findings accessors (503 when unconfigured).
  - service   the business logic (flatten, assess, persist) the routers call.
  - routers/  one module per surface (assess, connectors, datasets, findings, catalog).

main.py is the thin composition root: it builds `app`, includes these routers, and
exposes the probes. The container runs `uvicorn main:app`.
"""
