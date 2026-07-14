"""cryptohash — HMAC-SHA3-256 pseudonymisation action.

Replaces matched FHIR field values with a deterministic hex digest so identical
inputs always map to identical outputs (longitudinal linkage without leaking
originals).  Production deployments must set ``MEDANON_HASH_KEY``; plain
SHA3-256 is only permitted when ``MEDANON_HASH_ALLOW_PLAIN=true``.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json as _json_stdlib
import logging
import os
import threading
from typing import Any

from utils.fhirpath import find_nodes

_hash_log = logging.getLogger("medanon.cryptohash")
_warned_no_key = False
_warned_lock = threading.Lock()


def _normalized_node_str(value: Any) -> str:
    if isinstance(value, dict):
        # Keep historical serialization behavior for backward-compatible hashes.
        # Must use stdlib json.dumps (with spaces) — changing separators changes
        # the hash output and breaks all existing pseudonymized data.
        return _json_stdlib.dumps(value)
    return str(value)


def _resolve_secret_key(params: dict) -> str | None:
    """Return the HMAC secret key, scoped to the active data permit when one is set.

    Priority order (GDPR Art. 32 — secrets must not live in config files):
    1. Environment variable named by ``params['secret_key_env']``
    2. Environment variable ``MEDANON_HASH_KEY`` (global default)
    3. Inline ``params['secret_key']`` value (permitted only for local dev/testing)

    When a permit context is active (``utils.permit_context``), the
    resolved base key is HKDF-derived per permit so the same subject
    produces unrelated hashes under different permits (D7.2 §4.4). In
    regulated mode a permit context is required — see
    :func:`utils.permit_context.require_permit_if_regulated`.
    """
    env_name = params.get("secret_key_env")
    base_key = None
    if env_name:
        base_key = os.environ.get(str(env_name))
    if not base_key:
        base_key = os.environ.get("MEDANON_HASH_KEY")
    if not base_key:
        base_key = params.get("secret_key")
    if not base_key:
        return None

    from utils.permit_context import scope_key_to_permit

    return scope_key_to_permit(base_key, action="cryptohash")


def _compute_hash(msg: bytes, params: dict) -> str:
    hash_type = str(params.get("hash_type", "sha3_256")).lower()
    secret_key = _resolve_secret_key(params)

    if hash_type == "sha3_256":
        digestmod = "sha3_256"
    elif hash_type == "sha256":
        digestmod = "sha256"
    else:
        raise ValueError(f"Unsupported hash_type: {hash_type}")

    if secret_key:
        return _hmac.new(str(secret_key).encode(), msg, digestmod=digestmod).hexdigest()

    # Plain hashing without HMAC is dangerous — must be explicitly allowed, and
    # is never permitted in regulated mode (EHDS/D7.2 §4.4: an unsalted/unkeyed
    # hash of direct identifiers does not qualify as pseudonymisation).
    from utils.regulated import regulated_mode

    allow_plain = os.environ.get("MEDANON_HASH_ALLOW_PLAIN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if regulated_mode():
        raise ValueError(
            "MEDANON_REGULATED_MODE is on: plain hashing is not permitted. "
            "Set MEDANON_HASH_KEY for HMAC-based pseudonymization."
        )
    if not allow_plain:
        raise ValueError(
            "No HMAC key configured (MEDANON_HASH_KEY is unset) and "
            "MEDANON_HASH_ALLOW_PLAIN is not set to 'true'. "
            "Plain hashing is not permitted in production. "
            "Set MEDANON_HASH_KEY for HMAC-based pseudonymization, or set "
            "MEDANON_HASH_ALLOW_PLAIN=true for local testing."
        )

    global _warned_no_key
    with _warned_lock:
        if not _warned_no_key:
            _hash_log.warning(
                "No HMAC key configured (MEDANON_HASH_KEY is unset). "
                "Falling back to plain %s without a secret key. "
                "This produces deterministic, reversible hashes via rainbow tables — "
                "NOT suitable for production pseudonymization under GDPR Art. 4(5).",
                hash_type.upper(),
            )
            _warned_no_key = True
    return hashlib.new(digestmod, msg).hexdigest()


def _hash_nodes(node: Any, key: str, value: Any, params: dict) -> None:
    if isinstance(node, list):
        for item in node:
            _hash_nodes(item, key, value, params)
    elif isinstance(node, dict) and (key in node):
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    node_str = _normalized_node_str(node[key][idx])
                    node[key][idx] = _compute_hash(node_str.encode(), params)
        else:
            node_str = _normalized_node_str(node[key])
            node[key] = _compute_hash(node_str.encode(), params)


def cryptohash_by_path(resource: dict, el: dict, params: dict) -> None:
    ret = resource
    path = el["path"]  # "Patient.name"
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in cryptohash — "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(ret, path[:-1], [])
    _hash_nodes(ret, path[-1], el["value"], params)
