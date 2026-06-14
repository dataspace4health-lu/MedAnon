"""Synthetic data generation service."""

import asyncio
import logging
import os
from dataclasses import dataclass, field

from analytics.synthetic import (
    generate_synthetic_conditions,
    generate_synthetic_patients,
)
from pipeline.io_formats import parse_payload_bytes

try:
    from analytics.synthetic_sdv import (
        SDV_AVAILABLE,
        generate_synthetic_conditions_sdv,
        generate_synthetic_patients_sdv,
    )
except ImportError:
    SDV_AVAILABLE = False

logger = logging.getLogger("medanon")


@dataclass
class SyntheticResult:
    """Result of synthetic data generation."""

    patients: list[dict] = field(default_factory=list)
    conditions: list[dict] = field(default_factory=list)
    engine_used: str = "stdlib"


class SyntheticDataService:
    """Generates synthetic FHIR resources from de-identified input data."""

    async def generate(
        self,
        body: bytes,
        content_type: str,
        count: int = 100,
        seed: int | None = None,
        engine: str = "auto",
        include_conditions: bool = False,
        count_per_patient: int = 2,
        output_format: str = "ndjson",
    ) -> SyntheticResult | bytes:
        """Generate synthetic FHIR data.

        Returns ``SyntheticResult`` for local generation, or raw ``bytes``
        when proxying to the analytics microservice.

        Raises:
            ValueError: on validation or generation failure.
        """
        # Strangler Fig: proxy to analytics service when configured
        analytics_url = os.environ.get("ANALYTICS_SERVICE_URL", "")
        if analytics_url:
            from integrations.analytics.client import proxy_generate_synthetic

            params = {
                "count": count,
                "seed": seed,
                "engine": engine,
                "include_conditions": str(include_conditions).lower(),
                "count_per_patient": count_per_patient,
                "output_format": output_format,
            }
            return await asyncio.to_thread(
                proxy_generate_synthetic, body, content_type, params
            )

        # Local generation
        payload = parse_payload_bytes(body, content_type=content_type)

        from api.deps import _unwrap_to_resources

        resources = _unwrap_to_resources(payload)
        patients = [r for r in resources if r.get("resourceType") == "Patient"]

        if not patients:
            raise ValueError(
                "No Patient resources found in input — "
                "provide de-identified Patient FHIR resources"
            )

        use_sdv = self._resolve_engine(engine)

        if use_sdv:
            synthetic = await asyncio.to_thread(
                generate_synthetic_patients_sdv, patients, count=count, seed=seed
            )
        else:
            synthetic = await asyncio.to_thread(
                generate_synthetic_patients, patients, count=count, seed=seed
            )

        synthetic_conditions: list[dict] = []
        if include_conditions:
            conditions = [r for r in resources if r.get("resourceType") == "Condition"]
            if conditions:
                try:
                    if use_sdv:
                        synthetic_conditions = await asyncio.to_thread(
                            generate_synthetic_conditions_sdv,
                            conditions,
                            synthetic,
                            count_per_patient=count_per_patient,
                            seed=seed,
                        )
                    else:
                        synthetic_conditions = await asyncio.to_thread(
                            generate_synthetic_conditions,
                            conditions,
                            synthetic,
                            count_per_patient=count_per_patient,
                            seed=seed,
                        )
                except ValueError:
                    pass  # Silently skip if conditions input is insufficient

        return SyntheticResult(
            patients=synthetic,
            conditions=synthetic_conditions,
            engine_used="sdv" if use_sdv else "stdlib",
        )

    def _resolve_engine(self, engine: str) -> bool:
        """Resolve engine choice to use_sdv boolean."""
        if engine == "sdv":
            if not SDV_AVAILABLE:
                raise ValueError(
                    "SDV engine requested but sdv package is not installed. "
                    "Install with: pip install -r requirements-sdv.txt"
                )
            return True
        elif engine == "auto":
            return SDV_AVAILABLE
        elif engine == "stdlib":
            return False
        else:
            raise ValueError(f"Unknown engine '{engine}'. Choose: auto, sdv, stdlib")
