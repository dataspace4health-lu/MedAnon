"""Detection of reversal-capable rule actions (TEHDAS2 D7.2 §4.4, Art 66(3)).

Reversing pseudonymisation (``gpas_depseudonymize``) or decrypting an
``encrypt``-protected field (``decrypt``) re-exposes direct identifiers.
D7.2 §4.4 is explicit: "reversibility of the pseudonymisation can only be
implemented by the HDAB or a designated TTP and not by the data user"
(EHDS Art 66(3)). MedAnon's RBAC roles map the HDAB/TTP operator to
``admin`` and the data user to ``analyst`` (see ``api/auth.py``)  so a
config profile whose rules can reverse pseudonymisation/encryption must
only be executable by an ``admin`` caller.
"""

from __future__ import annotations

REVERSAL_ACTIONS = frozenset({"gpas_depseudonymize", "decrypt"})


def settings_has_reversal_actions(settings) -> bool:
    """True when *settings* (a loaded/runtime config Settings) can reverse
    pseudonymisation or decrypt a field.

    Accepts anything exposing a ``rules`` attribute of dict-shaped rules
    (``pipeline.config.service.Settings`` and
    ``api.schemas.processing.RuntimeSettings`` both qualify).
    """
    rules = getattr(settings, "rules", None) or []
    return any(
        isinstance(r, dict) and r.get("action") in REVERSAL_ACTIONS for r in rules
    )
