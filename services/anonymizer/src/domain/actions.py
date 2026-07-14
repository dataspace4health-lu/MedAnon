"""Canonical de-identification action names (the config vocabulary).

The set of valid action names is domain knowledge: config validation (rule
schema, AI config generator) needs it to reject unknown actions, and the engine
needs it to dispatch. Declaring it here - depended on downward by both the
pipeline and the adapters - avoids an adapter reaching up into
``pipeline.deidentify`` just to read the registry keys.

``pipeline.deidentify`` maps each of these names to its handler and asserts, at
import, that its dispatch dicts cover exactly these sets (drift guard), so this
stays the single source of truth for the vocabulary while the name -> function
mapping stays with the engine.
"""

from __future__ import annotations

DEIDENT_ACTION_NAMES = frozenset(
    {
        "redact",
        "perturb",
        "date_shift",
        "mask",
        "tokenize",
        "cryptohash",
        "substitute",
        "generalize",
        "scrub_text",
        "nlp_scrub",
        "nlp_detect",  # backward-compat alias of nlp_scrub
        "nlp_detect_act",
    }
)

PSEUDO_ACTION_NAMES = frozenset(
    {
        "gpas_pseudonymize",
        "encrypt",
    }
)

DEPSEUDO_ACTION_NAMES = frozenset(
    {
        "gpas_depseudonymize",
        "decrypt",
    }
)

ALL_ACTION_NAMES = DEIDENT_ACTION_NAMES | PSEUDO_ACTION_NAMES | DEPSEUDO_ACTION_NAMES
