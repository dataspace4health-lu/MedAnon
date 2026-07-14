"""integrations.gpas  gPAS TTP pseudonymisation service client.

Public API:
    client.py       ``gpas_pseudonymize_batch()``: retry + circuit breaker + L1/L2 cache
    adapter.py      ``GpasPseudonymizerAdapter`` implementing ``PseudonymizerPort``
    transport.py    low-level HTTP calls + connection pool
    circuit_breaker.py  3-state circuit breaker singleton
"""
