"""Fast JSON using orjson (Rust-based, 3-10x faster), with fallback to stdlib.

Use in hot paths (NDJSON parsing, HTTP bodies, batch processing).
Cold paths (config loading, job store) can stay on stdlib json.
"""

try:
    import orjson as _orjson

    def loads(data):
        """Parse JSON string or bytes → Python object."""
        return _orjson.loads(data)

    def dumps(obj) -> str:
        """Compact JSON string (no extra whitespace)."""
        return _orjson.dumps(obj).decode("utf-8")

    def dumps_sorted(obj) -> str:
        """Compact JSON string with keys sorted recursively — stable across runs.

        Use for dedup / cache keys where two equal dicts must hash identically
        regardless of insertion order.
        """
        return _orjson.dumps(obj, option=_orjson.OPT_SORT_KEYS).decode("utf-8")

    def dumps_bytes(obj) -> bytes:
        """Compact JSON bytes — use for HTTP request bodies (no decode overhead)."""
        return _orjson.dumps(obj)

    def dumps_pretty(obj) -> str:
        """Indented JSON string for human-readable output."""
        return _orjson.dumps(obj, option=_orjson.OPT_INDENT_2).decode("utf-8")

except ImportError:
    import json as _json

    def loads(data):
        return _json.loads(data)

    def dumps(obj) -> str:
        return _json.dumps(obj, separators=(",", ":"))

    def dumps_sorted(obj) -> str:
        """Compact JSON with keys sorted recursively — stable across runs."""
        return _json.dumps(obj, separators=(",", ":"), sort_keys=True)

    def dumps_bytes(obj) -> bytes:
        return _json.dumps(obj, separators=(",", ":")).encode("utf-8")

    def dumps_pretty(obj) -> str:
        return _json.dumps(obj, indent=2)
