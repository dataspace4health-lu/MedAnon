"""MedAnon API helpers — thin wrappers around requests."""
import os
import json
import requests

MEDANON_URL = os.environ.get("MEDANON_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = 60


def _get_auth_headers() -> dict:
    """Return dynamic auth headers (JWT or API key) from the auth module."""
    try:
        from utils.auth import get_auth_headers
        return get_auth_headers()
    except Exception:
        # Fallback: use static API key if auth module not available
        api_key = os.environ.get("MEDANON_API_KEY", "").strip()
        return {"X-API-Key": api_key} if api_key else {}


def health():
    """Return (ok: bool, detail: dict)."""
    try:
        r = requests.get(f"{MEDANON_URL}/health", timeout=5)
        return r.status_code == 200, r.json()
    except Exception as e:
        return False, {"error": str(e)}


def ready():
    """Return (ready: bool, checks: dict)."""
    try:
        r = requests.get(f"{MEDANON_URL}/ready", timeout=10)
        data = r.json()
        return data.get("ready", False), data.get("checks", {})
    except Exception as e:
        return False, {"error": str(e)}


def process_raw(content: str, output_format: str = "json", config_profile: str = "auto") -> tuple[bool, str]:
    """POST to /process/raw. Returns (ok, result_text)."""
    try:
        r = requests.post(
            f"{MEDANON_URL}/process/raw",
            data=content.encode(),
            params={"output_format": output_format, "config_profile": config_profile},
            headers={"Content-Type": "application/json", **_get_auth_headers()},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return True, r.text
        return False, r.text
    except Exception as e:
        return False, str(e)


def process_ndjson(content: bytes, timeout: int = TIMEOUT, config_profile: str = "auto"):
    """POST to /process/ndjson, streaming. Yields (ok: bool, line: str) tuples."""
    try:
        with requests.post(
            f"{MEDANON_URL}/process/ndjson",
            data=content,
            params={"config_profile": config_profile},
            headers={"Content-Type": "application/x-ndjson", **_get_auth_headers()},
            stream=True,
            timeout=timeout,
        ) as r:
            if r.status_code != 200:
                yield False, r.text
                return
            for raw_line in r.iter_lines():
                if raw_line:
                    yield True, raw_line.decode("utf-8")
    except Exception as e:
        yield False, str(e)


def process_batch(content: bytes, content_type: str = "application/x-ndjson", timeout: int = TIMEOUT, config_profile: str = "auto"):
    """POST to /process/batch (JSON, NDJSON, or XML). Yields (ok: bool, line: str) tuples."""
    try:
        with requests.post(
            f"{MEDANON_URL}/process/batch",
            data=content,
            params={"config_profile": config_profile},
            headers={"Content-Type": content_type, **_get_auth_headers()},
            stream=True,
            timeout=timeout,
        ) as r:
            if r.status_code != 200:
                yield False, r.text
                return
            for raw_line in r.iter_lines():
                if raw_line:
                    yield True, raw_line.decode("utf-8")
    except Exception as e:
        yield False, str(e)


def process_everything(server_url: str, resource_type: str, resource_id: str, timeout: int = TIMEOUT, config_profile: str = "auto"):
    """POST to /process/everything, streaming. Yields (ok: bool, line: str) tuples."""
    payload = {
        "server_url": server_url,
        "resource_type": resource_type,
        "resource_id": resource_id,
    }
    try:
        with requests.post(
            f"{MEDANON_URL}/process/everything",
            json=payload,
            params={"config_profile": config_profile},
            headers=_get_auth_headers(),
            stream=True,
            timeout=timeout,
        ) as r:
            if r.status_code != 200:
                yield False, r.text
                return
            for raw_line in r.iter_lines():
                if raw_line:
                    yield True, raw_line.decode("utf-8")
    except Exception as e:
        yield False, str(e)


def generate_synthetic(
    content: bytes,
    count: int = 100,
    content_type: str = "application/x-ndjson",
    seed: int | None = None,
    timeout: int = TIMEOUT,
) -> tuple[bool, bytes]:
    """POST de-identified FHIR patients to /generate/synthetic.

    Args:
        content: Raw bytes (NDJSON, JSON Bundle, or XML) containing Patient resources.
        count: Number of synthetic patients to request (1–10 000).
        content_type: MIME type of the input content.
        seed: Optional random seed for reproducible output.

    Returns (ok: bool, ndjson_bytes: bytes).
    """
    try:
        params: dict = {"count": count}
        if seed is not None:
            params["seed"] = seed
        r = requests.post(
            f"{MEDANON_URL}/generate/synthetic",
            data=content,
            params=params,
            headers={"Content-Type": content_type, **_get_auth_headers()},
            timeout=timeout,
        )
        if r.status_code == 200:
            return True, r.content
        return False, r.content
    except Exception as e:
        return False, str(e).encode()


def analyse_risk(content: bytes | str, content_type: str = "application/x-ndjson", timeout: int = TIMEOUT) -> tuple[bool, dict]:
    """POST FHIR content (JSON, NDJSON, or XML) to /analyse/risk.

    Args:
        content: Raw bytes or str. If str, encoded as UTF-8.
        content_type: MIME type — controls server-side format detection.

    Returns (ok: bool, report_or_error: dict).
    """
    try:
        data = content.encode("utf-8") if isinstance(content, str) else content
        r = requests.post(
            f"{MEDANON_URL}/analyse/risk",
            data=data,
            headers={"Content-Type": content_type, **_get_auth_headers()},
            timeout=timeout,
        )
        if r.status_code == 200:
            return True, r.json()
        return False, {"error": r.text, "status_code": r.status_code}
    except Exception as e:
        return False, {"error": str(e)}
