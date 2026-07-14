"""FhirAdapter  identity source adapter for FHIR JSON / NDJSON / XML.

FHIR is already the engine's intermediate representation, so this adapter is a
thin wrapper over :mod:`pipeline.io_formats`.  It exists so the FHIR path shares
the same :class:`~pipeline.sources.protocol.DataSourceAdapter` seam that the
future CDA / HL7v2 / DICOM / tabular adapters plug into  and so the output
stage can enforce "only FHIR may target the FHIR server" uniformly.
"""

from __future__ import annotations

from pipeline.exceptions import NormalizationError
from pipeline import io_formats


class FhirAdapter:
    """Identity adapter: FHIR in, FHIR out.

    Args:
        in_format:  ``"auto"`` | ``"json"`` | ``"ndjson"`` | ``"xml"``.
        out_format: serialization format for :meth:`serialize`.
    """

    source_format = "fhir"

    def __init__(self, in_format: str = "auto", out_format: str = "ndjson") -> None:
        self._in_format = in_format
        self._out_format = out_format

    def parse(self, raw: bytes | str) -> list[dict]:
        """Parse FHIR bytes into a list of resource dicts.

        A single resource, a Bundle, or a JSON array all normalise to a flat
        ``list[dict]`` so the result is uniform for ``process_data_batch``.
        """
        try:
            parsed = io_formats.parse_payload_bytes(raw, in_format=self._in_format)
        except ValueError as exc:
            raise NormalizationError(f"Invalid FHIR input: {exc}") from exc
        return self._to_resource_list(parsed)

    @staticmethod
    def _to_resource_list(parsed) -> list[dict]:
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        if isinstance(parsed, dict):
            # A Bundle becomes its entry resources; any other resource is a
            # singleton list.  (Bundle de-identification is handled by the
            # processor; here we only normalise the container shape.)
            if parsed.get("resourceType") == "Bundle" and isinstance(
                parsed.get("entry"), list
            ):
                resources = [
                    e["resource"]
                    for e in parsed["entry"]
                    if isinstance(e, dict) and isinstance(e.get("resource"), dict)
                ]
                return resources or [parsed]
            return [parsed]
        raise NormalizationError(
            f"Unexpected FHIR payload type: {type(parsed).__name__}"
        )

    def serialize(self, resources: list[dict]) -> bytes:
        """Serialize processed resources back to the configured FHIR format."""
        try:
            text, _content_type = io_formats.serialize_payload(
                resources, out_format=self._out_format
            )
        except ValueError as exc:
            raise NormalizationError(f"FHIR serialization failed: {exc}") from exc
        return text.encode("utf-8") if isinstance(text, str) else text

    def can_target_fhir_server(self) -> bool:
        # FHIR is the only format permitted to reach the target FHIR server.
        return True
