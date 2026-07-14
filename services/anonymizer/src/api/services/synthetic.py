"""Synthetic data generation service."""

import asyncio
import json
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
    dp_accounting: dict | None = None


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
        dp_epsilon: float | None = None,
    ) -> SyntheticResult | bytes:
        """Generate synthetic FHIR data.

        Returns ``SyntheticResult`` for local generation, or raw ``bytes``
        when proxying to the analytics microservice.

        When ``dp_epsilon`` is set the stdlib marginal engine is used and its
        marginals are differentially private; the achieved DP accounting is
        returned on ``SyntheticResult.dp_accounting``.

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
            if dp_epsilon is not None:
                params["dp_epsilon"] = dp_epsilon
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

        # DP synthesis is only defined for the stdlib marginal engine (SDV learns
        # a copula it would not respect our per-marginal noise). Force stdlib.
        use_sdv = False if dp_epsilon is not None else self._resolve_engine(engine)

        conditions = (
            [r for r in resources if r.get("resourceType") == "Condition"]
            if include_conditions
            else []
        )

        if dp_epsilon is not None:
            return await asyncio.to_thread(
                self._generate_dp,
                patients,
                conditions,
                count,
                seed,
                count_per_patient,
                dp_epsilon,
            )

        if use_sdv:
            synthetic = await asyncio.to_thread(
                generate_synthetic_patients_sdv, patients, count=count, seed=seed
            )
        else:
            synthetic = await asyncio.to_thread(
                generate_synthetic_patients, patients, count=count, seed=seed
            )

        synthetic_conditions: list[dict] = []
        if include_conditions and conditions:
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

    @staticmethod
    def _generate_dp(
        patients: list[dict],
        conditions: list[dict],
        count: int,
        seed: int | None,
        count_per_patient: int,
        dp_epsilon: float,
    ) -> SyntheticResult:
        """Differentially-private synthesis under one shared budget accountant.

        The budget is split evenly between the patient demographic marginals and
        (when present) the condition marginals, so the whole synthetic release
        satisfies ``dp_epsilon``-DP under sequential composition. Fail-closed: an
        overspend raises :class:`dp.PrivacyBudgetExceeded`.
        """
        from analytics import dp

        do_conditions = bool(conditions)
        patient_eps = dp_epsilon / 2 if do_conditions else dp_epsilon
        acc = dp.PrivacyAccountant(epsilon=dp_epsilon)

        synthetic = generate_synthetic_patients(
            patients, count=count, seed=seed, dp_epsilon=patient_eps, accountant=acc
        )
        synthetic_conditions: list[dict] = []
        if do_conditions:
            try:
                synthetic_conditions = generate_synthetic_conditions(
                    conditions,
                    synthetic,
                    count_per_patient=count_per_patient,
                    seed=seed,
                    dp_epsilon=dp_epsilon / 2,
                    accountant=acc,
                )
            except ValueError:
                pass  # Insufficient condition input — patients still DP-synthesised

        return SyntheticResult(
            patients=synthetic,
            conditions=synthetic_conditions,
            engine_used="stdlib-dp",
            dp_accounting=acc.summary(),
        )

    async def synthetic_passport(self, body: bytes, content_type: str) -> dict:
        """Fidelity + privacy passport for a synthetic dataset (D7.2 §5.4/§5.5).

        Body is a plain JSON object: ``{"real": [...], "synthetic": [...],
        "privacy_model": {...}?, "dp_params": {...}?}`` — not FHIR-format-detected
        content. Proxies when ANALYTICS_SERVICE_URL is set, else local.

        Raises:
            ValueError: on parse or proxy failure.
        """
        analytics_url = os.environ.get("ANALYTICS_SERVICE_URL", "")
        if analytics_url:
            from integrations.analytics.client import proxy_synthetic_passport

            return await asyncio.to_thread(proxy_synthetic_passport, body, content_type)

        def _local_passport():
            from analytics.synthetic_passport import build_synthetic_passport

            try:
                payload = json.loads(body) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except json.JSONDecodeError as exc:
                raise ValueError(f"Could not parse input: {exc}") from exc

            real = payload.get("real") or []
            synthetic = payload.get("synthetic") or []
            privacy_model = payload.get("privacy_model")
            dp_params = payload.get("dp_params")
            if not real or not synthetic:
                raise ValueError("both 'real' and 'synthetic' are required")
            return build_synthetic_passport(
                real, synthetic, privacy_model=privacy_model, dp_params=dp_params
            )

        return await asyncio.to_thread(_local_passport)

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
