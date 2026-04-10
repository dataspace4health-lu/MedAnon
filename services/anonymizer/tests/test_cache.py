"""Tests for utils/cache.py — pluggable gPAS result cache.

Covers:
- LocalLruCache: get/set, eviction at maxsize, thread safety
- configure_cache: injection replaces the module-level backend
- RedisCache: error swallowing on get/set failures
"""

import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch


class TestLocalLruCache(unittest.TestCase):
    def _make(self, maxsize=10):
        from utils.cache import LocalLruCache
        return LocalLruCache(maxsize=maxsize)

    def test_get_miss_returns_none(self):
        c = self._make()
        self.assertIsNone(c.get(("domain", "x")))

    def test_set_then_get(self):
        c = self._make()
        c.set(("domain", "abc"), "pseudo-abc")
        self.assertEqual(c.get(("domain", "abc")), "pseudo-abc")

    def test_eviction_at_maxsize(self):
        c = self._make(maxsize=10)
        for i in range(10):
            c.set(("d", str(i)), f"p{i}")
        # All 10 entries present
        self.assertEqual(c.get(("d", "9")), "p9")
        # Adding one more triggers eviction of oldest 10% (1 entry)
        c.set(("d", "10"), "p10")
        self.assertEqual(c.get(("d", "10")), "p10")
        # Total entries should be <= maxsize + 1 (we evicted before inserting)
        with c._lock:
            self.assertLessEqual(len(c._cache), 10)

    def test_eviction_removes_oldest_entries(self):
        c = self._make(maxsize=10)
        for i in range(10):
            c.set(("d", str(i)), f"p{i}")
        # Fill exactly; inserting one more should evict "0"
        c.set(("d", "overflow"), "px")
        # "0" is the oldest and should be gone
        self.assertIsNone(c.get(("d", "0")))

    def test_thread_safety(self):
        c = self._make(maxsize=200)
        errors = []

        def _worker(n):
            try:
                for i in range(20):
                    key = (f"t{n}", str(i))
                    c.set(key, f"v{n}_{i}")
                    c.get(key)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_get_promotes_to_most_recent(self):
        """Accessing a key via get() should move it to most-recently-used position,
        preventing eviction when newer keys fill the cache."""
        c = self._make(maxsize=10)
        for i in range(10):
            c.set(("d", str(i)), f"p{i}")
        # Access key "0" so it becomes the most recently used
        self.assertEqual(c.get(("d", "0")), "p0")
        # Insert a new key — triggers eviction of oldest 10% (1 entry)
        c.set(("d", "10"), "p10")
        # Key "0" should survive (was promoted), key "1" should be evicted (now oldest)
        self.assertEqual(c.get(("d", "0")), "p0")
        self.assertIsNone(c.get(("d", "1")))

    def test_set_existing_key_promotes(self):
        """Re-setting an existing key should move it to most-recently-used position
        without triggering eviction."""
        c = self._make(maxsize=10)
        for i in range(10):
            c.set(("d", str(i)), f"p{i}")
        # Re-set key "0" with a new value — should promote it
        c.set(("d", "0"), "p0_updated")
        # Insert a new key — should evict key "1" (now oldest), not "0"
        c.set(("d", "10"), "p10")
        self.assertEqual(c.get(("d", "0")), "p0_updated")
        self.assertIsNone(c.get(("d", "1")))

    def test_overwrite_existing_key(self):
        c = self._make()
        c.set(("d", "k"), "v1")
        c.set(("d", "k"), "v2")
        self.assertEqual(c.get(("d", "k")), "v2")


class TestConfigureCache(unittest.TestCase):
    def setUp(self):
        import utils.cache as cache_mod
        # Save original backend to restore after each test
        self._original = cache_mod._cache_backend
        self._mod = cache_mod

    def tearDown(self):
        # Restore original backend so other tests are not affected
        self._mod._cache_backend = self._original

    def test_configure_replaces_backend(self):
        from utils.cache import LocalLruCache, configure_cache
        new_backend = LocalLruCache(maxsize=5)
        configure_cache(new_backend)
        self.assertIs(self._mod._cache_backend, new_backend)

    def test_client_uses_injected_backend(self):
        """configure_cache() should affect what client.py uses at call time."""
        from utils.cache import LocalLruCache, configure_cache
        spy = LocalLruCache(maxsize=100)
        configure_cache(spy)
        # Write through the module-level backend directly
        self._mod._cache_backend.set(("DOMAIN", "orig123"), "PSEUDO456")
        self.assertEqual(self._mod._cache_backend.get(("DOMAIN", "orig123")), "PSEUDO456")

    def test_protocol_satisfied_by_local(self):
        from utils.cache import CacheBackend, LocalLruCache
        self.assertIsInstance(LocalLruCache(), CacheBackend)


def _install_fake_redis():
    """Inject a fake ``redis`` module so RedisCache can be instantiated without
    the real package installed.  Returns the fake StrictRedis class so tests
    can configure mock behaviour."""
    fake_redis_mod = types.ModuleType("redis")
    fake_strict = MagicMock(name="StrictRedis")
    fake_redis_mod.StrictRedis = fake_strict
    sys.modules["redis"] = fake_redis_mod
    return fake_strict


class TestRedisCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._fake_strict = _install_fake_redis()

    @classmethod
    def tearDownClass(cls):
        # Remove the fake module so it doesn't pollute other test modules
        sys.modules.pop("redis", None)

    def _make_with_mock_redis(self, get_return=None, get_raises=None, set_raises=None):
        """Build a RedisCache backed by a fresh mock client."""
        mock_client = MagicMock()
        if get_raises:
            mock_client.get.side_effect = get_raises
        else:
            mock_client.get.return_value = get_return
        if set_raises:
            mock_client.set.side_effect = set_raises

        # Make StrictRedis.from_url return our mock client
        self._fake_strict.from_url.return_value = mock_client

        # Force re-import so the lazy `import redis` inside __init__ picks up
        # our fake module
        sys.modules.pop("utils.cache", None)
        from utils.cache import RedisCache
        cache = RedisCache("redis://localhost:6379/0", ttl=60)
        cache._client = mock_client
        return cache, mock_client

    def test_get_returns_value(self):
        cache, mock_client = self._make_with_mock_redis(get_return="pseudonym-abc")
        result = cache.get(("DOMAIN", "orig"))
        self.assertEqual(result, "pseudonym-abc")

    def test_get_returns_none_on_miss(self):
        cache, mock_client = self._make_with_mock_redis(get_return=None)
        result = cache.get(("DOMAIN", "orig"))
        self.assertIsNone(result)

    def test_get_swallows_exception(self):
        cache, _ = self._make_with_mock_redis(get_raises=ConnectionError("redis down"))
        # Must NOT raise — falls back to None
        result = cache.get(("DOMAIN", "orig"))
        self.assertIsNone(result)

    def test_set_swallows_exception(self):
        cache, _ = self._make_with_mock_redis(set_raises=ConnectionError("redis down"))
        # Must NOT raise
        try:
            cache.set(("DOMAIN", "orig"), "pseudo")
        except Exception as exc:
            self.fail(f"set() raised unexpectedly: {exc}")

    def test_key_serialization(self):
        cache, mock_client = self._make_with_mock_redis()
        cache.get(("DOMAIN", "abc123"))
        called_key = mock_client.get.call_args[0][0]
        self.assertTrue(called_key.startswith("medanon:gpas:"))
        self.assertIn("DOMAIN", called_key)
        self.assertIn("abc123", called_key)

    def test_ttl_passed_to_set(self):
        cache, mock_client = self._make_with_mock_redis()
        cache.set(("DOMAIN", "orig"), "pseudo")
        mock_client.set.assert_called_once()
        _, kwargs = mock_client.set.call_args
        self.assertEqual(kwargs.get("ex"), 60)


if __name__ == "__main__":
    unittest.main()
