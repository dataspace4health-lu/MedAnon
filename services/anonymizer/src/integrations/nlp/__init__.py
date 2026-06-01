"""integrations.nlp — NLP microservice client (PHI detection).

Public API:
    remote_detector.py — ``detect_remote()``, ``detect_batch_remote()`` with
                          circuit breaker and fail-closed ``[NLP_UNAVAILABLE]`` fallback
    adapter.py         — ``RemoteNlpAdapter`` used by the pipeline
    utils.py           — Pure-Python helpers (entity resolution, tokenizer, XHTML scrubber)
"""
