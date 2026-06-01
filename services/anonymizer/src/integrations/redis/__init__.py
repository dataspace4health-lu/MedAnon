"""integrations.redis — Redis-backed job store and L2 cache.

Modules:
    job_store.py — ``RedisJobStore``: Redis Streams + consumer groups,
                   at-least-once delivery, XAUTOCLAIM for stale-job recovery.
"""
