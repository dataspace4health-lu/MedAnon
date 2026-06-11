"""perturb — date and numeric perturbation action.

Date perturbation
-----------------
Date values are shifted by a **deterministic per-subject offset** derived from
``HMAC-SHA256(MEDANON_HASH_KEY, subject_id)``.  The offset is reduced into the
configured ``[min, max]`` window so every date field belonging to the same
resource receives the **same** number-of-days shift.  This preserves:

  * temporal ordering  (date A < date B  →  perturbed(A) < perturbed(B))
  * intervals           (admit→discharge gap unchanged)
  * self-consistency    (the same calendar date in two fields maps identically)

Production deployments must set ``MEDANON_HASH_KEY``; plain SHA-256 (without
HMAC) is only permitted when ``MEDANON_HASH_ALLOW_PLAIN=true``.

Numeric perturbation
--------------------
Numeric values are perturbed by a cryptographically random offset
(``secrets.randbelow()``) per value — no ordering guarantee exists for numbers.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
import os
import threading
from datetime import timedelta
from typing import Any, Union

from utils.crypto import bounded_random
from utils.fhirpath import error, find_nodes, get_date

_log = logging.getLogger("medanon.perturb")
_warned_no_key = False
_warned_lock = threading.Lock()

expected_params = ["min", "max"]
date_format = "%Y-%m-%d"


def _subject_key(resource: dict) -> str:
    """Extract a stable subject identifier from a FHIR resource.

    Priority:
      1. resource.id  (the most common stable identifier)
      2. resource.subject.reference  (Observation, Condition, …)
      3. resource.patient.reference  (Encounter, …)
      4. empty string (no subject — offset is deterministic on the key value)
    """
    rid = resource.get("id")
    if rid:
        return str(rid)
    for ref_field in ("subject", "patient"):
        ref = resource.get(ref_field)
        if isinstance(ref, dict):
            ref_val = ref.get("reference")
            if ref_val:
                return str(ref_val)
    return ""


def _date_offset(subject_id: str, noise_range: list) -> int:
    """Return a deterministic day-offset for *subject_id* within *noise_range*.

    Uses HMAC-SHA256(MEDANON_HASH_KEY, subject_id) reduced modulo the span
    so the offset is stable across calls for the same subject and config.
    Falls back to plain SHA-256 when MEDANON_HASH_ALLOW_PLAIN=true and no key
    is set (same policy as cryptohash — warns once, never silently randomises).
    """
    global _warned_no_key

    low, high = int(noise_range[0]), int(noise_range[1])
    span = high - low
    if span < 0:
        raise ValueError(f"perturb: max ({high}) must be >= min ({low})")
    if span == 0:
        return low

    secret_key = os.environ.get("MEDANON_HASH_KEY") or ""
    msg = subject_id.encode()

    if secret_key:
        digest = _hmac.new(secret_key.encode(), msg, digestmod="sha256").digest()
    else:
        allow_plain = os.environ.get(
            "MEDANON_HASH_ALLOW_PLAIN", ""
        ).strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if not allow_plain:
            raise ValueError(
                "No HMAC key configured (MEDANON_HASH_KEY is unset) and "
                "MEDANON_HASH_ALLOW_PLAIN is not set to 'true'. "
                "Plain hashing is not permitted in production for date perturbation. "
                "Set MEDANON_HASH_KEY, or set MEDANON_HASH_ALLOW_PLAIN=true for local testing."
            )
        with _warned_lock:
            if not _warned_no_key:
                _log.warning(
                    "MEDANON_HASH_KEY is unset — date perturbation offsets are deterministic "
                    "but unkeyed (plain SHA-256). NOT suitable for production pseudonymization."
                )
                _warned_no_key = True
        digest = hashlib.sha256(msg).digest()

    # Reduce the first 8 bytes of the digest into [low, high]
    raw = int.from_bytes(digest[:8], "big")
    return low + (raw % (span + 1))


def _perturb_numeric(real_value: Union[int, float]) -> Any:
    """Random numeric noise — no ordering guarantee for numeric values."""
    # bounded_random uses secrets.randbelow() (CSPRNG); this is intentionally
    # non-deterministic because numeric values have no temporal ordering contract.
    # Callers must pass the noise range; we use ±10% as a sensible hard-coded
    # range for the fallback — but perturb_by_path passes the configured range.
    raise RuntimeError(
        "_perturb_numeric must not be called directly; use _perturb_nodes"
    )


def _perturb_nodes(
    node: Any, key: str, value: Any, noise_range: list, date_offset: int
) -> None:
    if isinstance(node, list):
        for item in node:
            _perturb_nodes(item, key, value, noise_range, date_offset)
    elif isinstance(node, dict) and (key in node):
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    elem = node[key][idx]
                    if isinstance(elem, (int, float)) and not isinstance(elem, bool):
                        # Numeric: CSPRNG noise (no ordering guarantee)
                        node[key][idx] = elem + bounded_random(
                            noise_range[0], noise_range[1]
                        )
                    elif get_date(elem, date_format):
                        # Date: deterministic per-subject offset
                        node[key][idx] = (
                            get_date(elem, date_format) + timedelta(days=date_offset)
                        ).strftime(date_format)
                    else:
                        error(f"{type(node[key][idx])} is not a number or date")
        else:
            elem = node[key]
            if isinstance(elem, (int, float)) and not isinstance(elem, bool):
                node[key] = elem + bounded_random(noise_range[0], noise_range[1])
            elif get_date(elem, date_format):
                node[key] = (
                    get_date(elem, date_format) + timedelta(days=date_offset)
                ).strftime(date_format)
            else:
                error(f"{type(node[key])} is not a number or date")


def perturb_by_path(resource: dict, el: dict, params: dict) -> None:
    """Perturb a numeric or date FHIR field matched by FHIRPath.

    Dates receive a **deterministic per-subject offset** (same offset for all
    date fields in the same resource) to preserve temporal ordering and intervals.
    Numeric values receive independent CSPRNG noise.

    Required params:
        min: minimum offset (days for dates, additive value for numbers)
        max: maximum offset (inclusive)

    Optional params:
        subject_id: override the subject key used for date-offset derivation
                    (default: resource.id → resource.subject.reference → "")
    """
    if not all(param in params for param in expected_params):
        error(f"Missing params (expected {expected_params})")
    path = el["path"]
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in perturb — "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    noise_range = [int(params["min"]), int(params["max"])]

    # Compute the deterministic date-offset once per perturb_by_path call.
    # All date fields in this resource use the same offset so temporal ordering
    # and inter-event intervals are preserved.
    sid = params.get("subject_id") or _subject_key(resource)
    d_offset = _date_offset(sid, noise_range)

    ret = find_nodes(resource, path[:-1], [])
    _perturb_nodes(ret, path[-1], el["value"], noise_range, d_offset)
