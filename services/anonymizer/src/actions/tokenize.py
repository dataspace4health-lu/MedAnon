"""tokenize — format-preserving deterministic pseudonymisation.

Replaces matched values with a synthetic token that **keeps the shape** of the
original (handy when downstream schemas validate the format of an MRN, account
number, etc.) while revealing nothing about the source value.

The token is a deterministic function of the value, so the **same input always
produces the same token** (within and across jobs, given the same key and
namespace) — enabling longitudinal linkage without a shared lookup table.
Different ``namespace`` values produce independent token spaces for the same
input, so an MRN and an account number that happen to be equal do not collide.

Format template
---------------
Each character of ``format`` is emitted literally **except** these placeholders:

  ``#``  → a digit ``0-9``
  ``@``  → an uppercase letter ``A-Z``
  ``*``  → an alphanumeric character ``0-9A-Z``

YAML
----
.. code-block:: yaml

    - name: tokenise medical record number
      match: "Patient.identifier.value"
      action: tokenize
      params:
        format: "PAT-######"     # PAT- followed by 6 derived digits
        namespace: "mrn"
        preserve_length: true    # only used when ``format`` is omitted

When ``format`` is omitted the token is alphanumeric; its length equals the
input length if ``preserve_length`` is true (default), else a fixed 12 chars.

Determinism is provided by ``HMAC-SHA256(MEDANON_HASH_KEY, namespace:value)``.
Production deployments must set ``MEDANON_HASH_KEY``; plain SHA-256 is only
permitted when ``MEDANON_HASH_ALLOW_PLAIN=true``.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
import os
import threading
from typing import Any

from utils.fhirpath import find_nodes

_log = logging.getLogger("medanon.tokenize")
_warned_no_key = False
_warned_lock = threading.Lock()

expected_params: list[str] = []

_DIGITS = "0123456789"
_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ALNUM = _DIGITS + _ALPHA
_DEFAULT_LENGTH = 12


def _derive_stream(namespace: str, value: str) -> "_Stream":
    """Return a deterministic byte stream keyed on (namespace, value)."""
    global _warned_no_key

    msg = f"{namespace}:{value}".encode()
    secret_key = os.environ.get("MEDANON_HASH_KEY") or ""
    if secret_key:
        seed = _hmac.new(secret_key.encode(), msg, digestmod="sha256").digest()
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
                "MEDANON_HASH_ALLOW_PLAIN is not set to 'true'. Plain hashing is "
                "not permitted in production for tokenization. Set MEDANON_HASH_KEY, "
                "or set MEDANON_HASH_ALLOW_PLAIN=true for local testing."
            )
        with _warned_lock:
            if not _warned_no_key:
                _log.warning(
                    "MEDANON_HASH_KEY is unset — tokenization is deterministic but "
                    "unkeyed (plain SHA-256). NOT suitable for production."
                )
                _warned_no_key = True
        seed = hashlib.sha256(msg).digest()

    return _Stream(seed)


class _Stream:
    """Deterministic, on-demand byte stream derived from a seed."""

    __slots__ = ("_seed", "_buf", "_counter")

    def __init__(self, seed: bytes) -> None:
        self._seed = seed
        self._buf = bytearray(seed)
        self._counter = 0

    def next_byte(self) -> int:
        if not self._buf:
            self._counter += 1
            self._buf = bytearray(
                hashlib.sha256(self._seed + self._counter.to_bytes(4, "big")).digest()
            )
        return self._buf.pop(0)


def _make_token(
    value: str, fmt: str | None, preserve_length: bool, namespace: str
) -> str:
    stream = _derive_stream(namespace, value)

    if fmt:
        chars: list[str] = []
        for ch in fmt:
            if ch == "#":
                chars.append(_DIGITS[stream.next_byte() % 10])
            elif ch == "@":
                chars.append(_ALPHA[stream.next_byte() % 26])
            elif ch == "*":
                chars.append(_ALNUM[stream.next_byte() % 36])
            else:
                chars.append(ch)
        return "".join(chars)

    length = len(value) if preserve_length else _DEFAULT_LENGTH
    if length <= 0:
        length = _DEFAULT_LENGTH
    return "".join(_ALNUM[stream.next_byte() % 36] for _ in range(length))


def _tokenize_nodes(node: Any, key: str, value: Any, params: dict) -> None:
    fmt = params.get("format")
    fmt = str(fmt) if fmt else None
    preserve_length = bool(params.get("preserve_length", True))
    namespace = str(params.get("namespace", "default"))

    if isinstance(node, list):
        for item in node:
            _tokenize_nodes(item, key, value, params)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value and isinstance(data, str):
                    node[key][idx] = _make_token(data, fmt, preserve_length, namespace)
        else:
            current = node[key]
            if isinstance(current, str) and (value is None or current == value):
                node[key] = _make_token(current, fmt, preserve_length, namespace)


def tokenize_by_path(resource: dict, el: dict, params: dict) -> None:
    path = el["path"]
    parts = path.split(".")[1:]  # strip resource-type root
    if len(parts) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in tokenize — "
            f"refusing to operate on entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(resource, parts[:-1], [])
    _tokenize_nodes(ret, parts[-1], el["value"], params)
