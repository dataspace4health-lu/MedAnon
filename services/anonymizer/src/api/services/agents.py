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
        ai_service_url = os.environ.get("AI_SERVICE_URL", "").strip()
        if ai_service_url:
            return await asyncio.to_thread(
                self._proxy_generate_config, ai_service_url, prompt, regulation,
            )
        from integrations.ai.agents.config_generator import generate_config

        return await asyncio.to_thread(generate_config, prompt, regulation)

    async def detect_pii(
        self, resources: list[dict], use_ai: bool = True,
    ) -> dict:
        ai_service_url = os.environ.get("AI_SERVICE_URL", "").strip()
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
        from integrations.ai.agents.compliance import advise_compliance

        return await asyncio.to_thread(advise_compliance, yaml_text, regulation)

    @staticmethod
    def _proxy_generate_config(
        base_url: str, prompt: str, regulation: str,
    ) -> dict:
        import json
        import urllib.request

        data = json.dumps({"prompt": prompt, "regulation": regulation}).encode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/ai/generate-config",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())

    @staticmethod
    def _proxy_detect_pii(
        base_url: str, resources: list[dict], use_ai: bool,
    ) -> dict:
        import json
        import urllib.request

        data = json.dumps({"resources": resources, "use_ai": use_ai}).encode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/ai/detect-pii",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
