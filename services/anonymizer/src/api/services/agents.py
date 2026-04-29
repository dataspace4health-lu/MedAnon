"""AI Agent service layer — orchestrates agent calls with async wrapping."""

import asyncio
import logging
import os

_log = logging.getLogger("medanon.ai")


class AgentService:
    """Stateless service coordinating AI agent operations."""

    async def get_status(self) -> dict:
        from integrations.ai.provider import is_ai_enabled

        if not is_ai_enabled():
            return {
                "enabled": False, "provider": "", "model": "",
                "api_base": "", "circuit_breaker": {}, "cache_size": 0,
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
            }
        except Exception:
            return {
                "enabled": True, "provider": "error", "model": "",
                "api_base": "", "circuit_breaker": {}, "cache_size": 0,
            }

    async def generate_config(self, prompt: str, regulation: str = "") -> dict:
        """Proxy or local — follows the ANALYTICS_SERVICE_URL pattern."""
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_generate_config, ai_service_url, prompt, regulation,
            )
        from integrations.ai.agents.config_generator import generate_config

        return await asyncio.to_thread(generate_config, prompt, regulation)

    async def detect_pii(
        self, resources: list[dict], use_ai: bool = True,
    ) -> dict:
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_detect_pii, ai_service_url, resources, use_ai,
            )
        from integrations.ai.agents.pii_detector import detect_pii_leaks

        return await asyncio.to_thread(detect_pii_leaks, resources, use_ai=use_ai)

    async def explain_config(
        self, yaml_text: str, regulation: str = "",
    ) -> str:
        """Returns explanation string."""
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_explain_config, ai_service_url, yaml_text, regulation,
            )
        from integrations.ai.agents.rule_explainer import (
            explain_config,
            explain_regulatory_alignment,
        )

        if regulation:
            return await asyncio.to_thread(
                explain_regulatory_alignment, yaml_text, regulation,
            )
        return await asyncio.to_thread(explain_config, yaml_text)

    async def advise_compliance(self, yaml_text: str, regulation: str) -> dict:
        ai_service_url = self._ai_service_url()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_advise_compliance, ai_service_url, yaml_text, regulation,
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

    @staticmethod
    def _post_proxy_json(base_url: str, path: str, payload: dict) -> dict:
        """POST JSON to the remote AI service and return the parsed dict.

        Centralised so all four agent proxies share request/response handling.
        """
        import json
        import urllib.request

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (SSRF tracked separately)
            raw = resp.read()
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
        parsed: dict, schema_cls, base_url: str, path: str,
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
        base_url: str, prompt: str, regulation: str,
    ) -> dict:
        from api.schemas.agents import ConfigGenerationResponse

        path = "/v1/ai/generate-config"
        parsed = AgentService._post_proxy_json(
            base_url, path, {"prompt": prompt, "regulation": regulation},
        )
        return AgentService._validate_response(
            parsed, ConfigGenerationResponse, base_url, path,
        )

    @staticmethod
    def _proxy_detect_pii(
        base_url: str, resources: list[dict], use_ai: bool,
    ) -> dict:
        from api.schemas.agents import PiiDetectionResponse

        path = "/v1/ai/detect-pii"
        parsed = AgentService._post_proxy_json(
            base_url, path, {"resources": resources, "use_ai": use_ai},
        )
        return AgentService._validate_response(
            parsed, PiiDetectionResponse, base_url, path,
        )

    @staticmethod
    def _proxy_explain_config(
        base_url: str, yaml_text: str, regulation: str,
    ) -> str:
        """Remote /v1/ai/explain returns a JSON object with an `explanation` field.

        Streaming is not proxied here — the SSE endpoint stays local.
        """
        path = "/v1/ai/explain"
        parsed = AgentService._post_proxy_json(
            base_url, path, {"yaml_text": yaml_text, "regulation": regulation},
        )
        explanation = parsed.get("explanation")
        if not isinstance(explanation, str):
            raise ValueError(
                f"AI service at {base_url}{path} response missing string `explanation` field",
            )
        return explanation

    @staticmethod
    def _proxy_advise_compliance(
        base_url: str, yaml_text: str, regulation: str,
    ) -> dict:
        """Remote /v1/ai/compliance returns the compliance analysis dict.

        Validated for shape (must be an object) but not by a strict schema
        because the result dict is intentionally open-ended (gaps, excess,
        recommendations vary by regulation).
        """
        path = "/v1/ai/compliance"
        return AgentService._post_proxy_json(
            base_url, path, {"yaml_text": yaml_text, "regulation": regulation},
        )
