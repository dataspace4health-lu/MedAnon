"""date_shift — consistent per-subject date jittering.

Shifts every matched date field by a **deterministic offset derived from an
anchor value** (e.g. the patient id).  Because the offset is a pure function of
the anchor, all date fields belonging to the same subject move by the *same*
number of days — preserving:

  * temporal ordering   (date A < date B  →  shifted(A) < shifted(B))
  * intervals           (admit→discharge gap unchanged)
  * cross-resource consistency (the same patient's dates shift identically in
    every resource, because they share the same anchor)

This differs from ``perturb`` in two ways:
  1. The anchor is configurable (``anchor_path``) rather than always the
     enclosing resource's subject — so an Observation can be shifted using the
     referenced Patient id, keeping it aligned with the Patient resource.
  2. The window is one-sided-configurable (``direction``: past / future / both).

YAML
----
.. code-block:: yaml

    - name: shift visit dates
      match: "Encounter.period.start"
      action: date_shift
      params:
        anchor_path: "Encounter.subject.reference"   # default: resource id
        max_days: 365
        direction: "past"            # "past" | "future" | "both"
        preserve_age_bracket: true   # cap shift so age-in-years is preserved

The offset is ``HMAC-SHA256(MEDANON_HASH_KEY, anchor) mod (max_days + 1)``,
sign chosen by ``direction``.  Production deployments must set
``MEDANON_HASH_KEY``; plain SHA-256 is only allowed with
``MEDANON_HASH_ALLOW_PLAIN=true`` — and never in regulated mode (EHDS/D7.2
§4.4). When a data-permit context is active (``pipeline.permit_context``)
the key is scoped per permit, so the same subject shifts by an unrelated
offset under different permits (§4.4: pseudonyms MUST NOT be reused across
permits) while staying deterministic within one permit.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
import os
import threading
from datetime import timedelta
from typing import Any

from utils.fhirpath import error, find_nodes, get_date

_log = logging.getLogger("medanon.date_shift")
_warned_no_key = False
_warned_lock = threading.Lock()

expected_params = ["max_days"]

# FHIR date / dateTime serialisations we attempt to parse, in priority order.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f%z",
)


def _anchor_value(resource: dict, anchor_path: str | None) -> str:
    """Resolve the anchor string used to derive the per-subject offset.

    ``anchor_path`` is a dotted FHIRPath (e.g. ``Patient.id`` or
    ``Observation.subject.reference``).  Falls back to ``resource.id`` and
    finally to an empty string (still deterministic on the matched values).
    """
    if anchor_path:
        parts = anchor_path.split(".")[1:]  # strip resource-type root
        if parts:
            try:
                nodes = find_nodes(resource, parts[:-1], [])
                for node in nodes if isinstance(nodes, list) else [nodes]:
                    if isinstance(node, dict) and parts[-1] in node:
                        val = node[parts[-1]]
                        if isinstance(val, list):
                            val = val[0] if val else None
                        if val:
                            return str(val)
            except Exception:  # noqa: BLE001 — anchor resolution is best-effort
                pass
    rid = resource.get("id")
    return str(rid) if rid else ""


def _signed_offset(anchor: str, max_days: int, direction: str) -> int:
    """Return a deterministic day-offset for *anchor* within the window."""
    global _warned_no_key

    if max_days <= 0:
        return 0

    secret_key = os.environ.get("MEDANON_HASH_KEY") or ""
    msg = anchor.encode()
    if secret_key:
        from pipeline.permit_context import scope_key_to_permit

        secret_key = scope_key_to_permit(secret_key, action="date_shift")
        digest = _hmac.new(secret_key.encode(), msg, digestmod="sha256").digest()
    else:
        from utils.regulated import regulated_mode

        allow_plain = os.environ.get(
            "MEDANON_HASH_ALLOW_PLAIN", ""
        ).strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if regulated_mode():
            raise ValueError(
                "MEDANON_REGULATED_MODE is on: plain hashing is not permitted. "
                "Set MEDANON_HASH_KEY for HMAC-based date shifting."
            )
        if not allow_plain:
            raise ValueError(
                "No HMAC key configured (MEDANON_HASH_KEY is unset) and "
                "MEDANON_HASH_ALLOW_PLAIN is not set to 'true'. Plain hashing is "
                "not permitted in production for date shifting. Set MEDANON_HASH_KEY, "
                "or set MEDANON_HASH_ALLOW_PLAIN=true for local testing."
            )
        with _warned_lock:
            if not _warned_no_key:
                _log.warning(
                    "MEDANON_HASH_KEY is unset — date-shift offsets are deterministic "
                    "but unkeyed (plain SHA-256). NOT suitable for production."
                )
                _warned_no_key = True
        digest = hashlib.sha256(msg).digest()

    raw = int.from_bytes(digest[:8], "big")

    if direction == "both":
        # Map into [-max_days, +max_days].
        span = 2 * max_days + 1
        return (raw % span) - max_days
    magnitude = raw % (max_days + 1)
    if direction == "future":
        return magnitude
    # default / "past"
    return -magnitude


def _parse_date(value: Any):
    if not isinstance(value, str):
        return None, None
    for fmt in _DATE_FORMATS:
        parsed = get_date(value, fmt)
        if parsed:
            return parsed, fmt
    return None, None


def _shift_one(value: Any, offset: int) -> Any:
    parsed, fmt = _parse_date(value)
    if parsed is None:
        error(f"date_shift: {value!r} is not a parseable FHIR date")
        return value
    shifted = parsed + timedelta(days=offset)
    if fmt.endswith("%z"):
        # strftime("%z") emits "+0200" (no colon), which is not a valid FHIR
        # dateTime offset; isoformat() keeps the required "+02:00" shape.
        return shifted.isoformat()
    return shifted.strftime(fmt)


def _shift_nodes(node: Any, key: str, value: Any, offset: int) -> None:
    if isinstance(node, list):
        for item in node:
            _shift_nodes(item, key, value, offset)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    node[key][idx] = _shift_one(data, offset)
        else:
            if value is None or node[key] == value:
                node[key] = _shift_one(node[key], offset)


def date_shift_by_path(resource: dict, el: dict, params: dict) -> None:
    if "max_days" not in params:
        error(f"date_shift missing params (expected {expected_params})")

    max_days = int(params["max_days"])
    direction = str(params.get("direction", "past")).lower()
    if direction not in ("past", "future", "both"):
        _log.warning("date_shift: invalid direction %r — using 'past'", direction)
        direction = "past"

    # preserve_age_bracket: keep the magnitude under one year so age-in-years is
    # preserved for the overwhelming majority of subjects.
    if bool(params.get("preserve_age_bracket", False)) and max_days > 364:
        max_days = 364

    path = el["path"]
    parts = path.split(".")[1:]  # strip resource-type root
    if len(parts) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in date_shift — "
            f"refusing to operate on entire resource (original path: {el['path']!r})"
        )

    anchor = _anchor_value(resource, params.get("anchor_path"))
    offset = _signed_offset(anchor, max_days, direction)

    ret = find_nodes(resource, parts[:-1], [])
    _shift_nodes(ret, parts[-1], el["value"], offset)
