"""Tests for utils/circuit_breaker.py — three-state circuit breaker.

Covers:
- CLOSED -> OPEN transition on threshold failures within window
- OPEN -> HALF_OPEN transition after recovery timeout
- HALF_OPEN -> CLOSED on probe success
- HALF_OPEN -> OPEN on probe failure
- Sliding window expiry resets failure count
- stats property returns correct snapshot
- reset() clears operational state but preserves total_trips
- Thread safety under concurrent record_failure / record_success
"""

import sys
import os
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from utils.circuit_breaker import CircuitBreaker


class TestClosedToOpen(unittest.TestCase):
    def setUp(self):
        self.cb = CircuitBreaker(
            name="test",
            failure_threshold=3,
            recovery_timeout_sec=10,
            window_sec=60,
        )

    def test_starts_closed(self):
        self.assertEqual(self.cb.state, CircuitBreaker.CLOSED)
        self.assertTrue(self.cb.allow_request())

    def test_failures_below_threshold_stay_closed(self):
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            self.cb.record_failure()
            self.cb.record_failure()
        self.assertEqual(self.cb.state, CircuitBreaker.CLOSED)
        self.assertTrue(self.cb.allow_request())

    def test_failures_at_threshold_open_circuit(self):
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            for _ in range(3):
                self.cb.record_failure()
            self.assertEqual(self.cb.state, CircuitBreaker.OPEN)
            self.assertFalse(self.cb.allow_request())

    def test_total_trips_increments_on_trip(self):
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            for _ in range(3):
                self.cb.record_failure()
        self.assertEqual(self.cb.stats["total_trips"], 1)


class TestOpenToHalfOpen(unittest.TestCase):
    def setUp(self):
        self.cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout_sec=5,
            window_sec=60,
            half_open_probes=2,
        )

    def _trip_open(self, t=100.0):
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = t
            self.cb.record_failure()
            self.cb.record_failure()

    def test_open_rejects_before_timeout(self):
        self._trip_open(t=100.0)
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 104.0
            self.assertFalse(self.cb.allow_request())
        self.assertEqual(self.cb.state, CircuitBreaker.OPEN)

    def test_open_transitions_to_half_open_after_timeout(self):
        self._trip_open(t=100.0)
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 105.0
            self.assertTrue(self.cb.allow_request())
        self.assertEqual(self.cb.state, CircuitBreaker.HALF_OPEN)

    def test_half_open_limits_probes(self):
        self._trip_open(t=100.0)
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 106.0
            self.assertTrue(self.cb.allow_request())
            self.assertTrue(self.cb.allow_request())
            self.assertFalse(self.cb.allow_request())


class TestHalfOpenToClosed(unittest.TestCase):
    def setUp(self):
        self.cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout_sec=5,
            window_sec=60,
            half_open_probes=2,
        )

    def _enter_half_open(self):
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            self.cb.record_failure()
            self.cb.record_failure()
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 106.0
            self.cb.allow_request()

    def test_success_closes_circuit(self):
        """All half_open_probes must succeed before circuit closes."""
        self._enter_half_open()
        self.assertEqual(self.cb.state, CircuitBreaker.HALF_OPEN)
        # Need both probes to succeed (half_open_probes=2)
        self.cb.allow_request()  # second probe slot
        self.cb.record_success()
        self.assertEqual(self.cb.state, CircuitBreaker.HALF_OPEN)  # still half-open after 1
        self.cb.record_success()
        self.assertEqual(self.cb.state, CircuitBreaker.CLOSED)     # closed after both

    def test_closed_after_probe_allows_all_requests(self):
        self._enter_half_open()
        self.cb.allow_request()  # second probe slot
        self.cb.record_success()
        self.cb.record_success()
        for _ in range(10):
            self.assertTrue(self.cb.allow_request())

    def test_failure_count_reset_on_success(self):
        self._enter_half_open()
        self.cb.allow_request()  # second probe slot
        self.cb.record_success()
        self.cb.record_success()
        self.assertEqual(self.cb.stats["failure_count"], 0)


class TestHalfOpenToOpen(unittest.TestCase):
    def setUp(self):
        self.cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout_sec=5,
            window_sec=60,
            half_open_probes=2,
        )

    def _enter_half_open(self):
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            self.cb.record_failure()
            self.cb.record_failure()
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 106.0
            self.cb.allow_request()

    def test_failure_during_half_open_reopens(self):
        self._enter_half_open()
        self.assertEqual(self.cb.state, CircuitBreaker.HALF_OPEN)
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 107.0
            self.cb.record_failure()
        self.assertEqual(self.cb.state, CircuitBreaker.OPEN)

    def test_total_trips_increments_on_reopen(self):
        self._enter_half_open()
        initial_trips = self.cb.stats["total_trips"]
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 107.0
            self.cb.record_failure()
        self.assertEqual(self.cb.stats["total_trips"], initial_trips + 1)

    def test_reopened_circuit_rejects_requests(self):
        self._enter_half_open()
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 107.0
            self.cb.record_failure()
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 108.0
            self.assertFalse(self.cb.allow_request())


class TestWindowExpiry(unittest.TestCase):
    def test_failures_outside_window_do_not_accumulate(self):
        cb = CircuitBreaker(
            name="test",
            failure_threshold=3,
            recovery_timeout_sec=10,
            window_sec=5,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()

        self.assertEqual(cb.state, CircuitBreaker.CLOSED)

        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 106.0
            cb.record_failure()

        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        self.assertEqual(cb.stats["failure_count"], 1)

    def test_failures_inside_window_accumulate(self):
        cb = CircuitBreaker(
            name="test",
            failure_threshold=3,
            recovery_timeout_sec=10,
            window_sec=10,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()
            cb.record_failure()

        self.assertEqual(cb.state, CircuitBreaker.OPEN)


class TestStats(unittest.TestCase):
    def test_initial_stats(self):
        cb = CircuitBreaker(
            name="my-breaker",
            failure_threshold=5,
            recovery_timeout_sec=30,
        )
        s = cb.stats
        self.assertEqual(s["name"], "my-breaker")
        self.assertEqual(s["state"], CircuitBreaker.CLOSED)
        self.assertEqual(s["failure_count"], 0)
        self.assertEqual(s["total_trips"], 0)
        self.assertEqual(s["threshold"], 5)
        self.assertEqual(s["recovery_timeout_sec"], 30)

    def test_stats_reflect_failures(self):
        cb = CircuitBreaker(
            name="s",
            failure_threshold=3,
            recovery_timeout_sec=10,
            window_sec=60,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()
        s = cb.stats
        self.assertEqual(s["failure_count"], 2)
        self.assertEqual(s["state"], CircuitBreaker.CLOSED)

    def test_stats_reflect_open_state(self):
        cb = CircuitBreaker(
            name="s",
            failure_threshold=2,
            recovery_timeout_sec=10,
            window_sec=60,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()
        s = cb.stats
        self.assertEqual(s["state"], CircuitBreaker.OPEN)
        self.assertEqual(s["total_trips"], 1)


class TestReset(unittest.TestCase):
    def test_reset_from_open(self):
        cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout_sec=10,
            window_sec=60,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.OPEN)
        cb.reset()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        self.assertEqual(cb.stats["failure_count"], 0)

    def test_reset_preserves_total_trips(self):
        cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout_sec=10,
            window_sec=60,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()
        trips_before = cb.stats["total_trips"]
        self.assertGreater(trips_before, 0)
        cb.reset()
        self.assertEqual(cb.stats["total_trips"], trips_before)

    def test_reset_allows_requests(self):
        cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout_sec=10,
            window_sec=60,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()
        cb.reset()
        self.assertTrue(cb.allow_request())

    def test_reset_from_closed_is_noop(self):
        cb = CircuitBreaker(name="test")
        cb.reset()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        self.assertEqual(cb.stats["total_trips"], 0)


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_failures_do_not_corrupt_state(self):
        cb = CircuitBreaker(
            name="threaded",
            failure_threshold=50,
            recovery_timeout_sec=60,
            window_sec=120,
        )
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    cb.record_failure()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(cb.state, CircuitBreaker.OPEN)
        self.assertGreaterEqual(cb.stats["total_trips"], 1)

    def test_concurrent_mixed_operations(self):
        cb = CircuitBreaker(
            name="mixed",
            failure_threshold=100,
            recovery_timeout_sec=60,
            window_sec=120,
        )
        errors = []
        barrier = threading.Barrier(8)

        def fail_worker():
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    cb.record_failure()
            except Exception as exc:
                errors.append(exc)

        def success_worker():
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    cb.record_success()
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=fail_worker) for _ in range(4)]
            + [threading.Thread(target=success_worker) for _ in range(4)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertIn(cb.state, {CircuitBreaker.CLOSED, CircuitBreaker.OPEN})

    def test_concurrent_allow_request_under_half_open(self):
        cb = CircuitBreaker(
            name="ho-threaded",
            failure_threshold=2,
            recovery_timeout_sec=1,
            window_sec=60,
            half_open_probes=3,
        )
        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 100.0
            cb.record_failure()
            cb.record_failure()

        allowed = []
        barrier = threading.Barrier(10)

        def probe_worker():
            barrier.wait(timeout=5)
            result = cb.allow_request()
            allowed.append(result)

        with patch("utils.circuit_breaker.time") as mock_time:
            mock_time.time.return_value = 200.0
            threads = [threading.Thread(target=probe_worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(sum(1 for a in allowed if a), 3)
        self.assertEqual(sum(1 for a in allowed if not a), 7)


if __name__ == "__main__":
    unittest.main()
