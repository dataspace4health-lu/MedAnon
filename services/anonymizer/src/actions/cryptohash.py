from utils.fhirpath import find_nodes
from Crypto.Hash import SHA3_256, SHA256, HMAC
import json as _json_stdlib
import logging
import os

_hash_log = logging.getLogger("medanon.cryptohash")
_warned_no_key = False


def _normalized_node_str(value):
    if isinstance(value, dict):
        # Keep historical serialization behavior for backward-compatible hashes.
        # Must use stdlib json.dumps (with spaces) — changing separators changes
        # the hash output and breaks all existing pseudonymized data.
        return _json_stdlib.dumps(value)
    return str(value)


def _resolve_secret_key(params):
    """Return the HMAC secret key.

    Priority order (GDPR Art. 32 — secrets must not live in config files):
    1. Environment variable named by ``params['secret_key_env']``
    2. Environment variable ``MEDANON_HASH_KEY`` (global default)
    3. Inline ``params['secret_key']`` value (permitted only for local dev/testing)
    """
    env_name = params.get("secret_key_env")
    if env_name:
        key = os.environ.get(str(env_name))
        if key:
            return key
    global_key = os.environ.get("MEDANON_HASH_KEY")
    if global_key:
        return global_key
    return params.get("secret_key")


def _compute_hash(msg, params):
    hash_type = str(params.get('hash_type', 'sha3_256')).lower()
    secret_key = _resolve_secret_key(params)

    if hash_type == 'sha3_256':
        digestmod = SHA3_256
    elif hash_type == 'sha256':
        digestmod = SHA256
    else:
        raise ValueError(f'Unsupported hash_type: {hash_type}')

    if secret_key:
        return HMAC.new(str(secret_key).encode(), msg, digestmod=digestmod).hexdigest()

    # Plain hashing without HMAC is dangerous — must be explicitly allowed
    allow_plain = os.environ.get("MEDANON_HASH_ALLOW_PLAIN", "").strip().lower() in (
        "1", "true", "yes",
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
    if not _warned_no_key:
        _hash_log.warning(
            "No HMAC key configured (MEDANON_HASH_KEY is unset). "
            "Falling back to plain %s without a secret key. "
            "This produces deterministic, reversible hashes via rainbow tables — "
            "NOT suitable for production pseudonymization under GDPR Art. 4(5).",
            hash_type.upper(),
        )
        _warned_no_key = True
    return digestmod.new(msg).hexdigest()


def _hash_nodes(node, key, value, params):
    if isinstance(node, list):
        for idx in range(len(node)):
            _hash_nodes(node[idx], key, value, params)
    elif isinstance(node, dict) and (key in list(node.keys())):
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    node_str = _normalized_node_str(node[key][idx])
                    node[key][idx] = _compute_hash(node_str.encode(), params)
        else:
            node_str = _normalized_node_str(node[key])
            node[key] = _compute_hash(node_str.encode(), params)

def cryptohash_by_path(resource, el, params):
    ret = resource
    path = el['path'] # "Patient.name"
    path = path.split('.')[1:] # Remove root
    if len(path) == 0:
        ret.clear()
        return
    ret = find_nodes(ret, path[:-1], [])
    _hash_nodes(ret, path[-1], el['value'], params)