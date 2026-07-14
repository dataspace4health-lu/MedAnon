"""decrypt  RSA-OAEP field decryption action.

Decrypts field values that were previously encrypted by the ``encrypt`` action,
using the private key at ``MEDANON_RSA_PRIVATE_KEY``.  Intended for
re-identification workflows where authorised parties need the original value.
"""

from __future__ import annotations

import json
from typing import Any

from utils.crypto import rsa_decrypt
from utils.fhirpath import error, find_nodes

supported_enc_schemes = {"RSA": rsa_decrypt}
expected_params = {"RSA": ["private_key"]}
encoding = "utf-8"


def _decrypt(ciphertext: bytes, enc_params: dict) -> Any:
    plaintext = supported_enc_schemes[enc_params["algorithm"]](ciphertext, enc_params)
    decoded = plaintext.decode(encoding)
    try:
        return json.loads(decoded)
    except (json.JSONDecodeError, ValueError):
        return decoded


def _decrypt_nodes(node: Any, key: str, value: Any, enc_params: dict) -> None:
    if isinstance(node, list):
        for item in node:
            _decrypt_nodes(item, key, value, enc_params)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    if isinstance(node[key][idx], dict):
                        node_str = json.dumps(node[key][idx])
                    else:
                        node_str = node[key][idx]
                    node[key][idx] = _decrypt(bytes.fromhex(node_str), enc_params)
        else:
            if isinstance(node[key], dict):
                node_str = json.dumps(node[key])
            else:
                node_str = node[key]
            node[key] = _decrypt(bytes.fromhex(node_str), enc_params)


def decrypt_by_path(resource: dict, el: dict, params: dict) -> None:
    algorithm = params.get("algorithm", "RSA")
    if algorithm not in supported_enc_schemes:
        error(
            f"Unsupported decryption algorithm: {algorithm!r}. Supported: {list(supported_enc_schemes)}"
        )
    params = {**params, "algorithm": algorithm}
    if not all(param in params for param in expected_params[algorithm]):
        error(f"Missing params (expected {expected_params[algorithm]})")
    ret = resource
    path = el["path"]
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in decrypt  "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(ret, path[:-1], [])
    _decrypt_nodes(ret, path[-1], el["value"], params)
