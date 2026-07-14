"""AI Agent service layer  orchestrates agent calls with async wrapping."""

import asyncio
import logging
import os

_log = logging.getLogger("medanon.ai")


class AgentService:
    """Stateless service coordinating AI agent operations."""

    async def get_status(self) -> dict:
        from integrations.ai.provider import is_ai_enabled

        from integrations.ai.agents.pii_detector import pii_enforcement_status

        if not is_ai_enabled():
            return {
                "enabled": False,
                "provider": "",
                "model": "",
                "api_base": "",
                "circuit_breaker": {},
                "cache_size": 0,
                "pii_enforcement": pii_enforcement_status(),
            }
        try:
            from integrations.ai.provider import get_provider

            provider = get_provider()
            stats = provider.stats
            model = stats["model"]
            return {
                "enabled": True,
                "provider": model.split("/")[0] if "/" in model else model,
                "model": model,
                "api_base": stats["api_base"],
                "circuit_breaker": stats["circuit_breaker"],
                "cache_size": stats["cache_size"],
                "pii_enforcement": pii_enforcement_status(),
            }
        except Exception:
            return {
                "enabled": True,
                "provider": "error",
                "model": "",
                "api_base": "",
                "circuit_breaker": {},
                "cache_size": 0,
                "pii_enforcement": pii_enforcement_status(),
            }

    async def generate_config(
        self,
        prompt: str,
        regulation: str = "",
        include_source_context: bool = False,
    ) -> dict:
        """Proxy or local  follows the ANALYTICS_SERVICE_URL pattern."""
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_generate_config,
                ai_service_url,
                prompt,
                regulation,
            )
        from integrations.ai.agents.config_generator import generate_config

        source_context = ""
        if include_source_context:
            source_context = await asyncio.to_thread(self._resolve_source_context)

        return await asyncio.to_thread(
            generate_config, prompt, regulation, source_context
        )

    @staticmethod
    def _resolve_source_context() -> str:
        """Fetch the PHI-free source-server resource snapshot (never raises)."""
        try:
            from integrations.ai.source_context import get_source_resource_context

            return get_source_resource_context()
        except Exception as exc:  # noqa: BLE001  context is best-effort
            _log.info("source_context_resolve_failed: %s", exc)
            return ""

    async def resolve_source_context_async(self) -> str:
        """Async wrapper around the source snapshot (runs off the event loop)."""
        return await asyncio.to_thread(self._resolve_source_context)

    async def detect_pii(
        self,
        resources: list[dict],
        use_ai: bool = True,
        min_len: int = 15,
    ) -> dict:
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_detect_pii,
                ai_service_url,
                resources,
                use_ai,
                min_len,
            )
        from integrations.ai.agents.pii_detector import detect_pii_leaks

        return await asyncio.to_thread(
            detect_pii_leaks, resources, use_ai=use_ai, min_len=min_len
        )

    async def scan_fields(
        self,
        field_context: str,
        model: str = "",
        *,
        granularity: str = "values",
        include_values: bool = False,
        guidance: str = "",
    ) -> dict:
        """Classify a field tree as PII and suggest actions.

        Runs the synchronous scanner agent off the event loop. Never raises
        the agent returns a structured error result on failure. ``granularity``
        ('values' | 'whole') controls leaf-vs-parent classification of
        structured fields. ``include_values`` marks that the tree carries sample
        values (PHI), so the agent enforces a local-only model. ``guidance`` is
        optional user instruction on how to treat fields.
        """
        from integrations.ai.agents.field_scanner import scan_fields

        return await asyncio.to_thread(
            scan_fields,
            field_context,
            model=model,
            granularity=granularity,
            include_values=include_values,
            guidance=guidance,
        )

    async def field_sketch(
        self,
        *,
        resource_types: list[str],
        resources: list[dict],
        n_per_type: int = 25,
        include_values: bool = False,
    ) -> dict:
        """Build a compact, PHI-safe field sketch for AI context.

        No model call: this is pure resource-to-schema compaction. When
        ``resources`` is supplied it sketches those (grouped by resourceType);
        otherwise it samples ``resource_types`` from the source FHIR server.
        Runs off the event loop (FHIR I/O + walking) and never raises.
        """
        from integrations.ai.agents.field_sketch import (
            build_field_sketch,
            group_by_resource_type,
            sample_source_and_sketch,
        )

        if resources:
            by_type = group_by_resource_type(resources)
            if not by_type:
                return {
                    "sketch": "",
                    "types": [],
                    "source": "error",
                    "detail": "no resources with a resourceType provided",
                }
            result = await asyncio.to_thread(
                build_field_sketch, by_type, include_values=include_values
            )
            result["source"] = "inline"
            result["detail"] = ""
            return result

        return await asyncio.to_thread(
            sample_source_and_sketch,
            resource_types,
            n_per_type=n_per_type,
            include_values=include_values,
        )

    async def explain_config(
        self,
        yaml_text: str,
        regulation: str = "",
    ) -> str:
        """Returns explanation string."""
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_explain_config,
                ai_service_url,
                yaml_text,
                regulation,
            )
        from integrations.ai.agents.rule_explainer import (
            explain_config,
            explain_regulatory_alignment,
        )

        if regulation:
            return await asyncio.to_thread(
                explain_regulatory_alignment,
                yaml_text,
                regulation,
            )
        return await asyncio.to_thread(explain_config, yaml_text)

    async def advise_compliance(self, yaml_text: str, regulation: str) -> dict:
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_advise_compliance,
                ai_service_url,
                yaml_text,
                regulation,
            )
        from integrations.ai.agents.compliance import advise_compliance

        return await asyncio.to_thread(advise_compliance, yaml_text, regulation)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _ai_service_url() -> str:
        """Return the trimmed AI_SERVICE_URL or empty string."""
        return os.environ.get("AI_SERVICE_URL", "").strip()

    # Hard cap on proxied AI-service responses (decompressed JSON bytes).
    _MAX_PROXY_RESPONSE_BYTES = 10 * 1024 * 1024

    @staticmethod
    def _validate_proxy_url(base_url: str) -> None:
        """SSRF guard for the AI proxy target (C6).

        ``AI_SERVICE_URL`` is admin-set env config (trusted, may legitimately
        point at an in-cluster private address), so private nets are allowed
        by default (MEDANON_AI_PROXY_ALLOW_PRIVATE=true). Link-local/metadata
        ranges and non-http(s) schemes are ALWAYS rejected  those are the
        cloud-metadata exfiltration vectors regardless of trust level.
        """
        import ipaddress
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"AI service URL must use http/https, got {parsed.scheme!r}",
            )
        host = parsed.hostname
        if not host:
            raise ValueError("AI service URL has no hostname")

        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addrs = [
                    ipaddress.ip_address(info[4][0])
                    for info in socket.getaddrinfo(
                        host, None, socket.AF_UNSPEC, socket.SOCK_STREAM
                    )
                ]
            except socket.gaierror:
                # Unresolvable → the request itself will fail; not an SSRF
                # vector (consistent with api/deps._validate_server_url).
                return

        blocked = (
            ipaddress.ip_network("169.254.0.0/16"),
            ipaddress.ip_network("fe80::/10"),
        )
        for addr in addrs:
            if any(addr in net for net in blocked):
                raise ValueError(
                    "AI service URL resolves to a link-local/metadata "
                    f"address ({addr})  refusing",
                )

        allow_private = os.environ.get(
            "MEDANON_AI_PROXY_ALLOW_PRIVATE", "true"
        ).strip().lower() in ("true", "1", "yes")
        if not allow_private:
            from utils.ssrf import check_hostname_ssrf

            err = check_hostname_ssrf(host)
            if err:
                raise ValueError(f"AI service URL rejected: {err}")

    @classmethod
    def _post_proxy_json(cls, base_url: str, path: str, payload: dict) -> dict:
        """POST JSON to the remote AI service and return the parsed dict.

        Centralised so all four agent proxies share request/response handling.
        Uses the pooled urllib3 client (no redirect following  the pool is
        built with retries=False) with an SSRF guard and a response-size cap.
        """
        import json

        from integrations.http_client import proxy_request

        cls._validate_proxy_url(base_url)
        data = json.dumps(payload).encode()
        resp = proxy_request(
            "POST",
            f"{base_url.rstrip('/')}{path}",
            body=data,
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        raw = resp.data
        if len(raw) > cls._MAX_PROXY_RESPONSE_BYTES:
            raise ValueError(
                f"AI service at {base_url}{path} returned oversized response "
                f"({len(raw)} bytes > {cls._MAX_PROXY_RESPONSE_BYTES})",
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"AI service at {base_url}{path} returned non-JSON response",
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"AI service at {base_url}{path} returned {type(parsed).__name__}, expected object",
            )
        return parsed

    @staticmethod
    def _validate_response(
        parsed: dict,
        schema_cls,
        base_url: str,
        path: str,
    ) -> dict:
        """Validate ``parsed`` against ``schema_cls`` and return a dict.

        Schemas use ``model_config = {"extra": "allow"}`` (or are explicitly
        configured) to tolerate forward-compatible additions; only structural
        violations or missing required fields trigger a ValueError.
        """
        from pydantic import ValidationError

        try:
            obj = schema_cls.model_validate(parsed)
        except ValidationError as exc:
            raise ValueError(
                f"AI service at {base_url}{path} returned malformed response: {exc.errors()[:3]}",
            ) from exc
        return obj.model_dump()

    @staticmethod
    def _proxy_generate_config(
        base_url: str,
        prompt: str,
        regulation: str,
    ) -> dict:
        from api.schemas.agents import ConfigGenerationResponse

        path = "/v1/ai/generate-config"
        parsed = AgentService._post_proxy_json(
            base_url,
            path,
            {"prompt": prompt, "regulation": regulation},
        )
        return AgentService._validate_response(
            parsed,
            ConfigGenerationResponse,
            base_url,
            path,
        )

    @staticmethod
    def _proxy_detect_pii(
        base_url: str,
        resources: list[dict],
        use_ai: bool,
        min_len: int = 15,
    ) -> dict:
        from api.schemas.agents import PiiDetectionResponse

        path = "/v1/ai/detect-pii"
        parsed = AgentService._post_proxy_json(
            base_url,
            path,
            {"resources": resources, "use_ai": use_ai, "min_field_len": min_len},
        )
        return AgentService._validate_response(
            parsed,
            PiiDetectionResponse,
            base_url,
            path,
        )

    @staticmethod
    def _proxy_explain_config(
        base_url: str,
        yaml_text: str,
        regulation: str,
    ) -> str:
        """Remote /v1/ai/explain returns a JSON object with an `explanation` field.

        Streaming is not proxied here  the SSE endpoint stays local.
        """
        path = "/v1/ai/explain"
        parsed = AgentService._post_proxy_json(
            base_url,
            path,
            {"yaml_text": yaml_text, "regulation": regulation},
        )
        explanation = parsed.get("explanation")
        if not isinstance(explanation, str):
            raise ValueError(
                f"AI service at {base_url}{path} response missing string `explanation` field",
            )
        return explanation

    @staticmethod
    def _proxy_advise_compliance(
        base_url: str,
        yaml_text: str,
        regulation: str,
    ) -> dict:
        """Remote /v1/ai/compliance returns the compliance analysis dict.

        Validated for shape (must be an object) but not by a strict schema
        because the result dict is intentionally open-ended (gaps, excess,
        recommendations vary by regulation).
        """
        path = "/v1/ai/compliance"
        return AgentService._post_proxy_json(
            base_url,
            path,
            {"yaml_text": yaml_text, "regulation": regulation},
        )
