"""encrypt — RSA-OAEP field encryption action.

Encrypts matched FHIR field values with the public key at
``MEDANON_RSA_PUBLIC_KEY``.  The ciphertext is Base64-encoded and written back
to the field.  Pairs with the ``decrypt`` action for authorised re-identification.
"""
from __future__ import annotations

import json
from typing import Any

from utils.crypto import rsa_encrypt
from utils.fhirpath import error, find_nodes

supported_enc_schemes = {"RSA": rsa_encrypt}
expected_params = {"RSA": ["public_key"]}
encoding = "utf-8"


def _encrypt(plaintext: bytes, enc_params: dict) -> str:
    ciphertext = supported_enc_schemes[enc_params["algorithm"]](plaintext, enc_params)
    return ciphertext.hex()


def _encrypt_nodes(node: Any, key: str, value: Any, enc_params: dict) -> None:
    if isinstance(node, list):
        for item in node:
            _encrypt_nodes(item, key, value, enc_params)
    elif isinstance(node, dict) and (key in node):
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    if isinstance(node[key][idx], dict):
                        node_str = json.dumps(node[key][idx])
                    else:
                        node_str = node[key][idx]
                    node[key][idx] = _encrypt(node_str.encode(encoding), enc_params)
        else:
            if isinstance(node[key], dict):
                node_str = json.dumps(node[key])
            else:
                node_str = node[key]
            node[key] = _encrypt(node_str.encode(encoding), enc_params)


def encrypt_by_path(resource: dict, el: dict, params: dict) -> None:
    algorithm = params.get("algorithm", "RSA")
    if algorithm not in supported_enc_schemes:
        error(
            f"Unsupported encryption algorithm: {algorithm!r}. Supported: {list(supported_enc_schemes)}"
        )
    params = {**params, "algorithm": algorithm}
    if not all(param in params for param in expected_params[algorithm]):
        error(f"Missing params (expected {expected_params[algorithm]})")
    ret = resource
    path = el["path"]
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in encrypt — "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(ret, path[:-1], [])
    _encrypt_nodes(ret, path[-1], el["value"], params)
