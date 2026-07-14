"""Public API for the NLP engine.

Re-exports the symbols used by main.py so callers import from a single
stable location.
"""

from recognizers import (
    HEALTHCARE_ENTITIES,
    _get_analyzer,
)
from tokenizer import (
    _GLOBAL_TOKEN_LOCK,
    _GLOBAL_TOKEN_STATE,
    _analyze_and_replace,
    _detect_entities,
    _detect_entities_cached,
    _scrub_xhtml_text_nodes,
    _tokenize,
    reset_detection_cache,
    reset_global_token_state,
    set_l2_cache,
)


def _resolve_entities(param) -> list:
    if param in (None, "healthcare"):
        return list(HEALTHCARE_ENTITIES)
    if param == "all":
        return list(HEALTHCARE_ENTITIES)
    if isinstance(param, str):
        return [e.strip() for e in param.split(",")]
    return list(param)
