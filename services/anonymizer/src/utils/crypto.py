"""utils.crypto  RSA encryption helpers, CSPRNG utilities, and permit-scoped
HMAC key derivation.

Provides RSA-OAEP encrypt/decrypt for the ``encrypt``/``decrypt`` actions,
``bounded_random()`` which uses ``secrets.randbelow()`` (CSPRNG) for
all random offsets, and ``derive_permit_key()`` which scopes a base HMAC
secret to a specific data permit (TEHDAS2 D7.2 §4.4: "Pseudonyms MUST NOT be
reused across different data permits").  Includes a path-traversal guard on
key file paths.

Public API:
    rsa_encrypt(plaintext, key_path)   encrypt bytes with a PEM public key
    rsa_decrypt(ciphertext, key_path)  decrypt bytes with a PEM private key
    bounded_random(low, high)          CSPRNG integer in [low, high)
    hash_key_id()                      active MEDANON_HASH_KEY_ID (rotation stamp)
    derive_permit_key(base, permit_id)  HKDF-SHA256 permit-scoped key (hex)
"""

import hashlib
import hmac as _hmac
import os
import secrets
import threading
from collections import OrderedDict

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization

# RSA key cache: bounded LRU keyed by resolved path → (mtime, key_object).
# Capped to prevent unbounded growth in deployments that rotate keys.
_KEY_CACHE_MAX = int(os.environ.get("MEDANON_RSA_KEY_CACHE_MAX", "32"))
_key_cache: "OrderedDict[str, tuple]" = OrderedDict()
_key_cache_lock = threading.Lock()


def _load_key(resolved_path, import_fn):
    """Load an RSA key from disk, returning a cached copy if the file hasn't changed."""
    mtime = os.path.getmtime(resolved_path)
    with _key_cache_lock:
        cached = _key_cache.get(resolved_path)
        if cached and cached[0] == mtime:
            # LRU touch
            _key_cache.move_to_end(resolved_path)
            return cached[1]
    with open(resolved_path, "rb") as fin:
        key = import_fn(fin.read())
    with _key_cache_lock:
        _key_cache[resolved_path] = (mtime, key)
        _key_cache.move_to_end(resolved_path)
        # Evict oldest entries if we exceeded the cap.
        while len(_key_cache) > _KEY_CACHE_MAX:
            _key_cache.popitem(last=False)
    return key


def _import_public_key(data: bytes):
    return serialization.load_pem_public_key(data)


def _import_private_key(data: bytes):
    return serialization.load_pem_private_key(data, password=None)


def bounded_random(min_val, max_val):
    """Return a cryptographically secure random integer in [min_val, max_val]."""
    span = max_val - min_val
    if span < 0:
        raise ValueError(f"max_val ({max_val}) must be >= min_val ({min_val})")
    return min_val + secrets.randbelow(span + 1)


def _validate_key_path(key_path):
    """Reject path-traversal attempts and restrict keys to allowed directories.

    Guards against:
    - Symlink/traversal attacks via realpath resolution
    - Reading arbitrary files by enforcing a directory allowlist
    """
    resolved = os.path.realpath(key_path)
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Key file not found: {key_path!r}")

    allowed_dirs_str = os.environ.get("MEDANON_KEY_ALLOWED_DIRS", "")
    if allowed_dirs_str:
        allowed_dirs = [
            os.path.realpath(d.strip())
            for d in allowed_dirs_str.split(":")
            if d.strip()
        ]
    else:
        # Default: allow /code/keys, /keys, and the config directory
        allowed_dirs = [
            os.path.realpath(d)
            for d in [
                "/code/keys",
                "/keys",
                os.environ.get("MEDANON_CONFIG_DIR", "/code/config"),
            ]
            if os.path.isdir(d)
        ]

    if not allowed_dirs:
        raise PermissionError(
            "No allowed key directories configured or found. "
            "Set MEDANON_KEY_ALLOWED_DIRS or ensure /code/keys or /keys exists."
        )

    if not any(resolved.startswith(d + os.sep) or resolved == d for d in allowed_dirs):
        raise PermissionError(
            f"Key file {key_path!r} resolves outside allowed directories: "
            f"{', '.join(allowed_dirs)}"
        )
    return resolved


_OAEP_PADDING = padding.OAEP(
    mgf=padding.MGF1(algorithm=hashes.SHA256()),
    algorithm=hashes.SHA256(),
    label=None,
)


def rsa_encrypt(plaintext, enc_params):
    key_path = _validate_key_path(enc_params["public_key"])
    public_key = _load_key(key_path, _import_public_key)
    if public_key.key_size < 2048:
        raise ValueError(
            f"RSA public key is {public_key.key_size} bits; minimum 2048 required"
        )
    return public_key.encrypt(plaintext, _OAEP_PADDING)


def rsa_decrypt(ciphertext, dec_params):
    key_path = _validate_key_path(dec_params["private_key"])
    private_key = _load_key(key_path, _import_private_key)
    if private_key.key_size < 2048:
        raise ValueError(
            f"RSA private key is {private_key.key_size} bits; minimum 2048 required"
        )
    return private_key.decrypt(ciphertext, _OAEP_PADDING)


# ---------------------------------------------------------------------------
# Key versioning + permit-scoped derivation (D7.2 §4.2 "key rotation", §4.4
# "pseudonyms MUST NOT be reused across different data permits")
# ---------------------------------------------------------------------------


def hash_key_id() -> str:
    """Return the active HMAC key-id stamp (``MEDANON_HASH_KEY_ID``, default 'v1').

    Recorded in the Transformation Passport and audit events so a future key
    rotation is detectable/attributable instead of silently invalidating
    longitudinal linkage.
    """
    return os.environ.get("MEDANON_HASH_KEY_ID", "v1").strip() or "v1"


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return _hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    t = b""
    okm = b""
    counter = 1
    while len(okm) < length:
        t = _hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        counter += 1
    return okm[:length]


_PERMIT_KDF_SALT = b"medanon-permit-scope-v1"


def derive_permit_key(base_key: str, *, permit_id: str, key_id: str = "") -> str:
    """Derive a permit-scoped HMAC secret from *base_key* via HKDF-SHA256 (RFC 5869).

    The same subject pseudonymised/tokenised/date-shifted under two different
    permits must produce *unrelated* outputs (D7.2 §4.4), while remaining
    deterministic *within* one permit so longitudinal linkage inside that
    permit's scope still works. ``key_id`` (see :func:`hash_key_id`) is mixed
    into the derivation so a key rotation changes every permit's derived key
    together, in a traceable way.

    Raises ``ValueError`` if *permit_id* is empty  callers must resolve the
    permit context before calling this (see ``utils.permit_context``).
    """
    if not permit_id:
        raise ValueError("derive_permit_key requires a non-empty permit_id")
    prk = _hkdf_extract(_PERMIT_KDF_SALT, base_key.encode())
    info = f"{key_id or hash_key_id()}:permit:{permit_id}".encode()
    return _hkdf_expand(prk, info, 32).hex()
