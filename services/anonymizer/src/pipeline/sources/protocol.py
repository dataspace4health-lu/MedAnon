"""DataSourceAdapter  structural Protocol for source-format adapters.

Mirrors the minimal-Protocol style of :class:`pipeline.ports.PseudonymizerPort`.
An adapter turns raw input bytes of one format into the common intermediate
representation (FHIR-shaped resource dicts) the rule engine consumes, and turns
processed IR back into that format's bytes.

Concrete implementations:

* :class:`~pipeline.sources.fhir_adapter.FhirAdapter`  JSON / NDJSON / XML (identity IR)
* (future) ``Hl7v2Adapter``, ``CdaAdapter``, ``DicomAdapter``, ``TabularAdapter``

Adapters raise :class:`pipeline.exceptions.NormalizationError` on malformed
input (replacing the bare ``ValueError`` the standalone ``formats/*`` scrubbers
raise today).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DataSourceAdapter(Protocol):
    """Structural interface for source-format normalization.

    ``source_format`` is a short identifier (``"fhir"``, ``"cda"``, …) used for
    logging, metrics, and adapter-registry lookup.
    """

    source_format: str

    def parse(self, raw: bytes | str) -> list[dict]:
        """Parse *raw* input into a list of intermediate-representation dicts.

        Raises :class:`pipeline.exceptions.NormalizationError` if *raw* is not
        well-formed for this adapter's format.
        """
        ...

    def serialize(self, resources: list[dict]) -> bytes:
        """Serialize processed IR *resources* back to this adapter's format."""
        ...

    def can_target_fhir_server(self) -> bool:
        """Whether this format's output may be uploaded to the FHIR target server.

        Only FHIR satisfies this.  The output stage uses it to enforce the
        invariant that non-FHIR output never reaches the target FHIR server.
        """
        ...
