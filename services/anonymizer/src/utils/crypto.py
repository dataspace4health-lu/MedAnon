import os
import secrets
import threading

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization

# RSA key cache: {resolved_path: (mtime, key_object)}
_key_cache = {}
_key_cache_lock = threading.Lock()


def _load_key(resolved_path, import_fn):
    """Load an RSA key from disk, returning a cached copy if the file hasn't changed."""
    mtime = os.path.getmtime(resolved_path)
    with _key_cache_lock:
        cached = _key_cache.get(resolved_path)
        if cached and cached[0] == mtime:
            return cached[1]
    with open(resolved_path, "rb") as fin:
        key = import_fn(fin.read())
    with _key_cache_lock:
        _key_cache[resolved_path] = (mtime, key)
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
