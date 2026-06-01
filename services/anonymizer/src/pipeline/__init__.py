"""pipeline — Core de-identification pipeline.

Orchestrated by ``pipeline/processor.py``:
    rule_matcher.py        — FHIRPath evaluation + per-resource rule index
    action_dispatcher.py   — Pass 1: action dispatch, NLP/gPAS work accumulation
    nlp_orchestrator.py    — Pass 1.5: batch NLP (detect → replace)
    gpas_orchestrator.py   — Pass 2: batch gPAS pseudonymisation
    post_processor.py      — Reference rewriting + text-ID replacement
    manifest.py            — Transformation manifest tagging
    deidentify.py          — Action implementations + NLP scrubbing helpers
"""
