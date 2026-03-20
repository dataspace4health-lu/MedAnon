import os
import secrets

from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP


def bounded_random(min_val, max_val):
    """Return a cryptographically secure random integer in [min_val, max_val]."""
    span = max_val - min_val
    if span < 0:
        raise ValueError(f"max_val ({max_val}) must be >= min_val ({min_val})")
    return min_val + secrets.randbelow(span + 1)


def _validate_key_path(key_path):
    """Reject path-traversal attempts by resolving the path and checking it exists."""
    resolved = os.path.realpath(key_path)
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Key file not found: {key_path!r}")
    return resolved


def rsa_encrypt(plaintext, enc_params):
    key_path = _validate_key_path(enc_params['public_key'])
    with open(key_path, encoding='utf-8') as fin:
        pub_key_data = fin.read()
    public_key = RSA.import_key(pub_key_data)
    cipher_rsa = PKCS1_OAEP.new(public_key)
    return cipher_rsa.encrypt(plaintext)


def rsa_decrypt(ciphertext, dec_params):
    key_path = _validate_key_path(dec_params['private_key'])
    with open(key_path, encoding='utf-8') as fin:
        priv_key_data = fin.read()
    private_key = RSA.import_key(priv_key_data)
    cipher_rsa = PKCS1_OAEP.new(private_key)
    return cipher_rsa.decrypt(ciphertext)
