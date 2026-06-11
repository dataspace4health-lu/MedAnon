"""mask — partial value masking action.

Unlike ``redact`` (which removes the whole field) ``mask`` keeps a recognisable
structure while hiding the sensitive characters.  Useful for phone numbers,
emails, credit-card-like identifiers, and any value where downstream consumers
need format/shape but not the actual data.

Strategies
----------
``keep_prefix``  keep the first ``keep_chars`` characters, mask the rest
                 ``+33612345678`` → ``+336********`` (keep_chars=4)
``keep_suffix``  keep the last ``keep_chars`` characters, mask the rest
                 ``4111111111111234`` → ``************1234`` (keep_chars=4)
``keep_domain``  email-aware — mask local part, keep ``@domain``
                 ``user@hospital.fr`` → ``****@hospital.fr``
``keep_country_code``  phone-aware — keep a leading ``+NN`` country code,
                 mask the national number
                 ``+33612345678`` → ``+33*********``
``full``         mask every character (equivalent to redact but length-preserving)

Common params
-------------
``mask_char``       character used for masking (default ``"*"``)
``keep_chars``      number of chars to preserve for prefix/suffix (default ``4``)
``preserve_length`` when ``True`` (default) the masked region keeps the original
                    length; when ``False`` it collapses to a fixed marker
"""

from __future__ import annotations

import logging
from typing import Any

from utils.fhirpath import find_nodes

_log = logging.getLogger("medanon.mask")

expected_params = ["strategy"]

_DEFAULT_MASK_CHAR = "*"
_DEFAULT_KEEP_CHARS = 4
_VALID_STRATEGIES = frozenset(
    {"keep_prefix", "keep_suffix", "keep_domain", "keep_country_code", "full"}
)


def _mask_run(length: int, mask_char: str, preserve_length: bool) -> str:
    """Return the masked substring for *length* hidden characters."""
    if length <= 0:
        return ""
    if preserve_length:
        return mask_char * length
    # Collapse to a fixed-width marker so output length does not leak the
    # original value length.
    return mask_char * 3


def _mask_value(value: str, strategy: str, params: dict) -> str:
    mask_char = str(params.get("mask_char", _DEFAULT_MASK_CHAR)) or _DEFAULT_MASK_CHAR
    keep_chars = int(params.get("keep_chars", _DEFAULT_KEEP_CHARS))
    preserve_length = bool(params.get("preserve_length", True))

    if keep_chars < 0:
        keep_chars = 0

    if strategy == "full":
        return _mask_run(len(value), mask_char, preserve_length)

    if strategy == "keep_prefix":
        if keep_chars >= len(value):
            # Nothing left to hide — fail closed: mask the whole value rather
            # than leak a short identifier verbatim.
            return _mask_run(len(value), mask_char, preserve_length)
        kept = value[:keep_chars]
        return kept + _mask_run(len(value) - keep_chars, mask_char, preserve_length)

    if strategy == "keep_suffix":
        if keep_chars >= len(value):
            return _mask_run(len(value), mask_char, preserve_length)
        kept = value[len(value) - keep_chars :]
        return _mask_run(len(value) - keep_chars, mask_char, preserve_length) + kept

    if strategy == "keep_domain":
        # Email-aware: mask the local part, keep "@domain".
        at = value.rfind("@")
        if at <= 0:
            # No local part to keep — mask the whole thing.
            return _mask_run(len(value), mask_char, preserve_length)
        local = value[:at]
        domain = value[at:]  # includes "@"
        return _mask_run(len(local), mask_char, preserve_length) + domain

    if strategy == "keep_country_code":
        # Phone-aware: keep a leading "+NN" (or "+N") country code, mask the rest.
        if value.startswith("+"):
            # Country codes are 1-3 digits after the "+".
            idx = 1
            while idx < len(value) and idx <= 3 and value[idx].isdigit():
                idx += 1
            kept = value[:idx]
            return kept + _mask_run(len(value) - idx, mask_char, preserve_length)
        # No country code present — fall back to keep_prefix semantics.
        return _mask_value(value, "keep_prefix", params)

    # Unknown strategy — fail safe by masking everything.
    _log.warning("mask: unknown strategy %r — masking full value", strategy)
    return _mask_run(len(value), mask_char, preserve_length)


def _mask_nodes(node: Any, key: str, value: Any, strategy: str, params: dict) -> None:
    if isinstance(node, list):
        for item in node:
            _mask_nodes(item, key, value, strategy, params)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value and isinstance(data, str):
                    node[key][idx] = _mask_value(data, strategy, params)
        else:
            current = node[key]
            if isinstance(current, str) and (value is None or current == value):
                node[key] = _mask_value(current, strategy, params)


def mask_by_path(resource: dict, el: dict, params: dict) -> None:
    strategy = str(params.get("strategy", "keep_suffix"))
    if strategy not in _VALID_STRATEGIES:
        _log.warning(
            "mask: invalid strategy %r (valid: %s) — defaulting to keep_suffix",
            strategy,
            ", ".join(sorted(_VALID_STRATEGIES)),
        )
        strategy = "keep_suffix"

    path = el["path"]  # "Patient.telecom.value"
    parts = path.split(".")[1:]  # Remove resource-type root
    if len(parts) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in mask — "
            f"refusing to mask entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(resource, parts[:-1], [])
    _mask_nodes(ret, parts[-1], el["value"], strategy, params)
