"""Risk analysis service — orchestrates local or proxied risk assessment."""

import asyncio
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
