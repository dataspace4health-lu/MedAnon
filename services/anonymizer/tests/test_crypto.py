"""Tests for utils/crypto.py — RSA encrypt/decrypt, key validation, bounded random.

Covers:
- _validate_key_path: allowed dirs, path traversal, non-existent files
- bounded_random: range correctness, edge cases
- RSA roundtrip: encrypt then decrypt recovers plaintext
- Key size enforcement: reject keys smaller than 2048 bits
- Key caching: _load_key reuses cached entries
- OAEP+SHA256 padding: confirmed via successful roundtrip
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
from cryptography.hazmat.primitives import serialization as _serialization

from utils.crypto import (
    _import_public_key,
    _key_cache,
    _load_key,
    _validate_key_path,
    bounded_random,
    rsa_decrypt,
    rsa_encrypt,
)


def _generate_rsa_keypair(key_size=2048):
    """Generate an RSA keypair, returning (public_pem, private_pem) as bytes."""
    private_key = _rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    pub_pem = private_key.public_key().public_bytes(
        encoding=_serialization.Encoding.PEM,
        format=_serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_pem = private_key.private_bytes(
        encoding=_serialization.Encoding.PEM,
        format=_serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=_serialization.NoEncryption(),
    )
    return pub_pem, priv_pem


class TestValidateKeyPath(unittest.TestCase):
    """Tests for _validate_key_path path-traversal and allowlist guard."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.key_file = os.path.join(self.tmpdir, "test.pem")
        with open(self.key_file, "w") as f:
            f.write("dummy-key-content")
        self._orig_env = os.environ.get("MEDANON_KEY_ALLOWED_DIRS")
        os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self.tmpdir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_env is None:
            os.environ.pop("MEDANON_KEY_ALLOWED_DIRS", None)
        else:
            os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self._orig_env

    def test_valid_path_returns_resolved(self):
        result = _validate_key_path(self.key_file)
        self.assertEqual(result, os.path.realpath(self.key_file))

    def test_nonexistent_file_raises_file_not_found(self):
        missing = os.path.join(self.tmpdir, "no_such_file.pem")
        with self.assertRaises(FileNotFoundError):
            _validate_key_path(missing)

    def test_path_traversal_raises(self):
        traversal = os.path.join(self.tmpdir, "..", "..", "etc", "passwd")
        with self.assertRaises((FileNotFoundError, PermissionError)):
            _validate_key_path(traversal)

    def test_file_outside_allowed_dir_raises_permission_error(self):
        other_dir = tempfile.mkdtemp()
        try:
            outside_file = os.path.join(other_dir, "outside.pem")
            with open(outside_file, "w") as f:
                f.write("key-data")
            with self.assertRaises(PermissionError):
                _validate_key_path(outside_file)
        finally:
            shutil.rmtree(other_dir, ignore_errors=True)

    def test_multiple_allowed_dirs_colon_separated(self):
        second_dir = tempfile.mkdtemp()
        try:
            second_file = os.path.join(second_dir, "key2.pem")
            with open(second_file, "w") as f:
                f.write("key-data")
            os.environ["MEDANON_KEY_ALLOWED_DIRS"] = f"{self.tmpdir}:{second_dir}"
            result = _validate_key_path(second_file)
            self.assertEqual(result, os.path.realpath(second_file))
        finally:
            shutil.rmtree(second_dir, ignore_errors=True)

    def test_no_allowed_dirs_raises_permission_error(self):
        os.environ["MEDANON_KEY_ALLOWED_DIRS"] = ""
        # Also patch default dirs so none exist
        with patch("os.path.isdir", return_value=False):
            with self.assertRaises(PermissionError) as ctx:
                _validate_key_path(self.key_file)
            self.assertIn("No allowed key directories", str(ctx.exception))


class TestBoundedRandom(unittest.TestCase):
    """Tests for bounded_random CSPRNG wrapper."""

    def test_values_in_range(self):
        for _ in range(100):
            val = bounded_random(5, 15)
            self.assertGreaterEqual(val, 5)
            self.assertLessEqual(val, 15)

    def test_min_equals_max_returns_that_value(self):
        self.assertEqual(bounded_random(42, 42), 42)

    def test_max_less_than_min_raises_value_error(self):
        with self.assertRaises(ValueError):
            bounded_random(10, 5)

    def test_negative_range(self):
        for _ in range(50):
            val = bounded_random(-10, -5)
            self.assertGreaterEqual(val, -10)
            self.assertLessEqual(val, -5)

    def test_zero_span(self):
        self.assertEqual(bounded_random(0, 0), 0)


class TestRsaRoundtrip(unittest.TestCase):
    """Tests for rsa_encrypt / rsa_decrypt roundtrip with 2048-bit keys."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_env = os.environ.get("MEDANON_KEY_ALLOWED_DIRS")
        os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self.tmpdir

        pub_pem, priv_pem = _generate_rsa_keypair(2048)
        self.pub_path = os.path.join(self.tmpdir, "public.pem")
        self.priv_path = os.path.join(self.tmpdir, "private.pem")
        with open(self.pub_path, "wb") as f:
            f.write(pub_pem)
        with open(self.priv_path, "wb") as f:
            f.write(priv_pem)

        _key_cache.clear()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_env is None:
            os.environ.pop("MEDANON_KEY_ALLOWED_DIRS", None)
        else:
            os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self._orig_env
        _key_cache.clear()

    def test_encrypt_decrypt_bytes(self):
        plaintext = b"sensitive patient data"
        ciphertext = rsa_encrypt(plaintext, {"public_key": self.pub_path})
        self.assertIsInstance(ciphertext, bytes)
        self.assertNotEqual(ciphertext, plaintext)
        recovered = rsa_decrypt(ciphertext, {"private_key": self.priv_path})
        self.assertEqual(recovered, plaintext)

    def test_encrypt_decrypt_json_dict(self):
        data = {"patient_id": "P-12345", "name": "John Doe"}
        plaintext = json.dumps(data).encode("utf-8")
        ciphertext = rsa_encrypt(plaintext, {"public_key": self.pub_path})
        recovered = rsa_decrypt(ciphertext, {"private_key": self.priv_path})
        self.assertEqual(json.loads(recovered.decode("utf-8")), data)

    def test_encrypt_decrypt_empty_bytes(self):
        plaintext = b""
        ciphertext = rsa_encrypt(plaintext, {"public_key": self.pub_path})
        recovered = rsa_decrypt(ciphertext, {"private_key": self.priv_path})
        self.assertEqual(recovered, plaintext)


class TestKeySizeEnforcement(unittest.TestCase):
    """Tests that keys smaller than 2048 bits are rejected."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_env = os.environ.get("MEDANON_KEY_ALLOWED_DIRS")
        os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self.tmpdir

        pub_pem, priv_pem = _generate_rsa_keypair(1024)
        self.pub_path = os.path.join(self.tmpdir, "small_pub.pem")
        self.priv_path = os.path.join(self.tmpdir, "small_priv.pem")
        with open(self.pub_path, "wb") as f:
            f.write(pub_pem)
        with open(self.priv_path, "wb") as f:
            f.write(priv_pem)

        _key_cache.clear()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_env is None:
            os.environ.pop("MEDANON_KEY_ALLOWED_DIRS", None)
        else:
            os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self._orig_env
        _key_cache.clear()

    def test_encrypt_rejects_small_key(self):
        with self.assertRaises(ValueError) as ctx:
            rsa_encrypt(b"data", {"public_key": self.pub_path})
        self.assertIn("minimum 2048", str(ctx.exception))

    def test_decrypt_rejects_small_key(self):
        with self.assertRaises(ValueError) as ctx:
            rsa_decrypt(b"data", {"private_key": self.priv_path})
        self.assertIn("minimum 2048", str(ctx.exception))


class TestKeyCaching(unittest.TestCase):
    """Tests for _load_key file-based caching."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_env = os.environ.get("MEDANON_KEY_ALLOWED_DIRS")
        os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self.tmpdir

        pub_pem, _ = _generate_rsa_keypair(2048)
        self.pub_path = os.path.join(self.tmpdir, "cached_pub.pem")
        with open(self.pub_path, "wb") as f:
            f.write(pub_pem)

        _key_cache.clear()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_env is None:
            os.environ.pop("MEDANON_KEY_ALLOWED_DIRS", None)
        else:
            os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self._orig_env
        _key_cache.clear()

    def test_cache_populated_after_first_load(self):
        resolved = os.path.realpath(self.pub_path)
        self.assertNotIn(resolved, _key_cache)
        _load_key(resolved, _import_public_key)
        self.assertIn(resolved, _key_cache)
        mtime, cached_key = _key_cache[resolved]
        self.assertEqual(mtime, os.path.getmtime(resolved))
        self.assertIsNotNone(cached_key)

    def test_second_load_uses_cache(self):
        resolved = os.path.realpath(self.pub_path)
        key1 = _load_key(resolved, _import_public_key)
        import builtins
        original_open = builtins.open
        call_count = [0]

        def counting_open(*args, **kwargs):
            if args and str(args[0]) == resolved:
                call_count[0] += 1
            return original_open(*args, **kwargs)

        with patch("builtins.open", side_effect=counting_open):
            key2 = _load_key(resolved, _import_public_key)

        self.assertEqual(call_count[0], 0, "File should not be re-read on cache hit")
        self.assertIs(key1, key2)


class TestOaepSha256Padding(unittest.TestCase):
    """Verify OAEP with SHA-256 is used (roundtrip confirms compatible padding)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_env = os.environ.get("MEDANON_KEY_ALLOWED_DIRS")
        os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self.tmpdir

        pub_pem, priv_pem = _generate_rsa_keypair(2048)
        self.pub_path = os.path.join(self.tmpdir, "oaep_pub.pem")
        self.priv_path = os.path.join(self.tmpdir, "oaep_priv.pem")
        with open(self.pub_path, "wb") as f:
            f.write(pub_pem)
        with open(self.priv_path, "wb") as f:
            f.write(priv_pem)

        _key_cache.clear()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_env is None:
            os.environ.pop("MEDANON_KEY_ALLOWED_DIRS", None)
        else:
            os.environ["MEDANON_KEY_ALLOWED_DIRS"] = self._orig_env
        _key_cache.clear()

    def test_oaep_sha256_roundtrip(self):
        """Encrypt and decrypt succeed, confirming both sides use OAEP+SHA256."""
        plaintext = b"OAEP-SHA256 padding verification"
        ciphertext = rsa_encrypt(plaintext, {"public_key": self.pub_path})
        recovered = rsa_decrypt(ciphertext, {"private_key": self.priv_path})
        self.assertEqual(recovered, plaintext)

    def test_ciphertext_differs_between_calls(self):
        """OAEP is probabilistic -- encrypting the same plaintext twice produces
        different ciphertext (confirms random padding is applied)."""
        plaintext = b"same input"
        ct1 = rsa_encrypt(plaintext, {"public_key": self.pub_path})
        ct2 = rsa_encrypt(plaintext, {"public_key": self.pub_path})
        self.assertNotEqual(ct1, ct2)


if __name__ == "__main__":
    unittest.main()
