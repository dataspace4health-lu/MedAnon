"""Risk analysis service — orchestrates local or proxied risk assessment."""

import asyncio
import json
import logging
import os

from analytics.risk import assess_risk_resources
from pipeline.io_formats import parse_payload_bytes

logger = logging.getLogger("medanon")


class RiskAnalysisService:
    """Computes re-identification risk metrics on FHIR resources."""

    async def analyse_risk(self, body: bytes, content_type: str) -> dict:
        """Run risk analysis — proxy when ANALYTICS_SERVICE_URL is set, else local.

        Returns the risk report dict.

        Raises:
            ValueError: on parse, validation, or proxy failure.
        """
        analytics_url = os.environ.get("ANALYTICS_SERVICE_URL", "")
        if analytics_url:
            from integrations.analytics.client import proxy_analyse_risk

            return await asyncio.to_thread(proxy_analyse_risk, body, content_type)

        def _local_analyse():
            payload = parse_payload_bytes(body, content_type=content_type)
            from api.deps import _unwrap_to_resources

            resources = _unwrap_to_resources(payload)
            return assess_risk_resources(resources)

        return await asyncio.to_thread(_local_analyse)

    async def analyse_privacy_risk(self, body: bytes, content_type: str) -> dict:
        """Run the WS1/D7.2 §5.5.7 privacy-risk assessment.

        Unlike :meth:`analyse_risk`, the body is a plain JSON object
        (``{"resources": [...], "synthetic": [...]?, "privacy_model": {...}?}``),
        not FHIR-format-detected content — proxy when ANALYTICS_SERVICE_URL is
        set, else local.

        Raises:
            ValueError: on parse or proxy failure.
        """
        analytics_url = os.environ.get("ANALYTICS_SERVICE_URL", "")
        if analytics_url:
            from integrations.analytics.client import proxy_analyse_privacy_risk

            return await asyncio.to_thread(
                proxy_analyse_privacy_risk, body, content_type
            )

        def _local_analyse():
            from analytics.privacy_risk import assess_privacy_risk

            try:
                payload = json.loads(body) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except json.JSONDecodeError as exc:
                raise ValueError(f"Could not parse input: {exc}") from exc

            resources = payload.get("resources") or []
            rows = payload.get("rows")
            synthetic = payload.get("synthetic")
            privacy_model = payload.get("privacy_model")
            return assess_privacy_risk(
                resources, rows=rows, synthetic=synthetic, privacy_model=privacy_model
            )

        return await asyncio.to_thread(_local_analyse)
