"""HTTP client for a FHIR validation server returning an ``OperationOutcome``.

Targets the HL7 / Inferno **FHIR validator-wrapper** standalone server
(``POST /validate?profile=...`` → OperationOutcome; ``GET /version`` readiness)
 see https://github.com/inferno-community/fhir-validator-wrapper. Any endpoint
that returns a FHIR ``OperationOutcome`` works, including a FHIR server's
``[base]/{type}/$validate`` (set ``TRUST_GATE_VALIDATOR_ENDPOINT`` to
``/{type}/$validate``).

Fail-soft: when the validator is unreachable (or the breaker is open) the client
raises :class:`ValidatorUnavailable` so the conformance checks mark themselves
"not assessed" (NA) rather than emit a false PASS.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from urllib.parse import urlparse

import urllib3

from breaker import CircuitBreaker

_log = logging.getLogger("trust_gate.validator")

_HTTP: urllib3.PoolManager | None = None


def _http() -> urllib3.PoolManager:
    global _HTTP
    if _HTTP is None:
        _HTTP = urllib3.PoolManager(num_pools=4, maxsize=8, retries=False)
    return _HTTP


class ValidatorUnavailable(RuntimeError):
    """Raised when the FHIR validator cannot be reached (transport/outage).

    Trips the circuit breaker and degrades the affected check to NA. Reserve this
    for *service-level* failures (connection refused, timeout, DNS)  never for a
    per-request error from a validator that did respond (see ValidatorBadRequest).
    """


class ValidatorBadRequest(RuntimeError):
    """Raised when a *responding* validator rejected one specific request.

    The classic case: ``POST /validate?profile=<url>`` where the validator has no
    such profile loaded -> HTTP 500 "Unable to resolve profile". The service is
    healthy; only this resource/profile pair is unassessable. This must NOT trip
    the breaker or zero out sibling checks (e.g. base-spec structural validation),
    so it is a distinct type the caller skips per-request.
    """


def _guard_url(url: str) -> None:
    """SSRF guard: only http(s); block link-local/loopback-metadata literals.

    Sidecars resolve to private DNS by design, so private RFC1918 ranges are
    allowed; only block clearly dangerous targets (e.g. the cloud metadata IP).
    Set ``TRUST_GATE_ALLOW_ANY_HOST=true`` to disable.
    """
    if os.environ.get("TRUST_GATE_ALLOW_ANY_HOST", "").lower() in ("1", "true", "yes"):
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidatorUnavailable(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname or ""
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (socket.gaierror, ValueError):
        return  # unresolvable name → let the HTTP call fail/circuit-break normally
    if ip.is_link_local or str(ip) == "169.254.169.254":
        raise ValidatorUnavailable(f"blocked link-local/metadata target: {ip}")


class ValidatorClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        endpoint: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Endpoint path; ``{type}`` (if present) is substituted with resourceType.
        self._endpoint = endpoint or os.environ.get(
            "TRUST_GATE_VALIDATOR_ENDPOINT", "/validate"
        )
        self._breaker = CircuitBreaker(
            name="trust_gate_validator",
            failure_threshold=int(
                os.environ.get("TRUST_GATE_VALIDATOR_CB_THRESHOLD", "5")
            ),
            recovery_timeout_sec=float(
                os.environ.get("TRUST_GATE_VALIDATOR_CB_RECOVERY_SEC", "30")
            ),
        )

    def _url(self, resource_type: str) -> str:
        path = self._endpoint.replace("{type}", resource_type or "Resource")
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    def is_ready(self) -> bool:
        """Best-effort readiness probe (``GET /version``)."""
        try:
            resp = _http().request("GET", f"{self._base_url}/version", timeout=5.0)
            return resp.status < 500
        except Exception:  # noqa: BLE001
            return False

    def validate(self, resource: dict, profiles: list[str] | None = None) -> list[dict]:
        """Return the error/fatal ``OperationOutcome`` issues for *resource*.

        An empty list means the resource is conformant (no error/fatal issues).
        """
        if not self._breaker.allow_request():
            raise ValidatorUnavailable("circuit breaker open")

        url = self._url(resource.get("resourceType", ""))
        if profiles:
            # Append profile as a query-string parameter; don't mix urllib3
            # fields (form-encoding) with a raw body  urllib3 rejects that.
            profile_qs = "&".join(f"profile={p}" for p in profiles)
            url = f"{url}?{profile_qs}"
        _guard_url(url)
        try:
            resp = _http().request(
                "POST",
                url,
                body=json.dumps(resource).encode("utf-8"),
                headers={
                    "Content-Type": "application/fhir+json",
                    "Accept": "application/fhir+json",
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._breaker.record_failure()
            raise ValidatorUnavailable(f"transport error: {exc}") from exc

        if resp.status in (502, 503, 504):
            # Infra/proxy "service unavailable" codes -> genuine outage. Trip the
            # breaker so the affected check degrades to NA.
            self._breaker.record_failure()
            raise ValidatorUnavailable(f"upstream {resp.status}")
        if resp.status >= 400:
            # The validator answered with an application error (most often a 500
            # "Unable to resolve profile" for a ?profile= it has not loaded). The
            # service is healthy, so count breaker success and surface a per-request
            # error the caller skips for this one resource -- base-spec structural
            # validation of the other resources stays intact, no false outage.
            self._breaker.record_success()
            body = resp.data[:200].decode("utf-8", "replace") if resp.data else ""
            raise ValidatorBadRequest(f"upstream {resp.status}: {body.strip()}")

        try:
            outcome = json.loads(resp.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._breaker.record_failure()
            raise ValidatorUnavailable(f"invalid JSON: {exc}") from exc

        self._breaker.record_success()
        # Structural/profile/IG all exclude terminology  code/binding validity is
        # measured solely by conformance.terminology (its own tx client), so tx
        # state never distorts the structure axis. See _error_issues.
        return _error_issues(outcome)


# The validator runs terminology validation (code/display/value-set binding) in
# the SAME call as structural/profile validation and tags those issues with a
# ``TerminologyEngine`` source. Terminology validity has its own dedicated check
# (``conformance.terminology``), so terminology issues must NOT also drive the
# structural OR profile verdict (double-counting + wrong dimension). This also
# means a terminology-server outage  where the engine escalates "couldn't check
# this code" to a hard ``code-invalid`` error  and over-strict binding quirks
# (e.g. ``text/plain; charset=utf-8`` vs the ``MimeType`` value set) never block
# on the structure axis. See _is_terminology_issue / _is_transport_issue.
_TERMINOLOGY_SOURCE = "terminologyengine"
# Substrings that mark a validator/terminology INFRASTRUCTURE failure (tx server
# unreachable, slow, or returning a non-FHIR error page) rather than a data
# finding. Matched case-insensitively against the issue detail text. Kept
# specific to transport/IO phrasing so genuine structural messages (which may
# contain words like "connection" or a bare "timeout") are not swept up.
_TRANSPORT_ERROR_MARKERS = (
    "error from http",
    "unparseable",
    "sockettimeout",
    "java.net.",
    "read timed out",
    "connect timed out",
    "unable to connect",
)


def _issue_source(issue: dict) -> str:
    """Return the lowercased validator engine that raised *issue* (or "").

    Handles the FHIR ``OperationOutcome`` shape (an ``issue-source`` extension
    carrying a ``valueString``) and the validator-wrapper shape (a bare
    ``source`` key).
    """
    for ext in issue.get("extension", []) or []:
        if isinstance(ext, dict) and str(ext.get("url", "")).endswith("issue-source"):
            return str(ext.get("valueString", "")).strip().lower()
    return str(issue.get("source", "")).strip().lower()


def _issue_detail_text(issue: dict) -> str:
    """Return the human-readable detail text of *issue* (lowercased)."""
    details = issue.get("details")
    if isinstance(details, dict) and details.get("text"):
        return str(details["text"]).lower()
    return str(issue.get("diagnostics") or issue.get("message") or "").lower()


def _is_terminology_issue(issue: dict) -> bool:
    """True if *issue* was raised by the validator's terminology engine."""
    return _issue_source(issue) == _TERMINOLOGY_SOURCE


def _is_transport_issue(issue: dict) -> bool:
    """True if *issue* is a validator/terminology INFRASTRUCTURE failure.

    The tx server was unreachable/slow or returned a non-FHIR page (e.g.
    "Error from http://tx...: Unparseable HTML", a socket timeout). These mean
    the code could not be checked  never a data defect  so they are dropped
    even when (rarely) the issue carries no engine source tag.
    """
    text = _issue_detail_text(issue)
    return any(marker in text for marker in _TRANSPORT_ERROR_MARKERS)


def _error_issues(outcome: dict) -> list[dict]:
    """Extract STRUCTURAL error/fatal issues from an OperationOutcome.

    Handles both a bare ``OperationOutcome`` (``issue[]``) and the HL7
    validator-wrapper envelope (``outcomes[].issues[]`` with ``level``).

    Terminology is EXCLUDED entirely (both base-spec and profile/IG calls):
    code, display, and value-set-binding validity are measured separately by the
    dedicated ``conformance.terminology`` check (its own tx client), so they must
    not also drive structural/profile conformance. This keeps two things honest:

    * a terminology-server outage can never read as a data defect (the
      ``TerminologyEngine`` "couldn't check this code" errors are dropped); and
    * over-strict binding quirks (e.g. ``text/plain; charset=utf-8`` not matching
      the ``MimeType`` value set) do not block on the structural/profile axis.

    What remains is what the instance validator actually asserts about shape:
    cardinality, data types, required-element presence, slicing, invariants,
    must-support. Infrastructure/transport errors are always dropped too.
    """
    issues: list[dict] = []
    if isinstance(outcome, dict) and isinstance(outcome.get("issue"), list):
        issues = [
            i
            for i in outcome["issue"]
            if isinstance(i, dict) and i.get("severity") in ("error", "fatal")
        ]
    elif isinstance(outcome, dict) and isinstance(outcome.get("outcomes"), list):
        for o in outcome["outcomes"]:
            for i in (o.get("issues") or []) if isinstance(o, dict) else []:
                if isinstance(i, dict) and str(i.get("level", "")).upper() in (
                    "ERROR",
                    "FATAL",
                ):
                    issues.append(i)

    return [
        i for i in issues if not _is_terminology_issue(i) and not _is_transport_issue(i)
    ]


def get_validator_client() -> ValidatorClient | None:
    url = os.environ.get("TRUST_GATE_VALIDATOR_URL", "").strip()
    if not url:
        return None
    timeout = float(os.environ.get("TRUST_GATE_VALIDATOR_TIMEOUT_SEC", "15"))
    return ValidatorClient(url, timeout=timeout)
