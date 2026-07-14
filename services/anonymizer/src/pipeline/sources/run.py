"""run_through_engine — drive any DataSourceAdapter through the full pipeline.

One helper that ties a :class:`~pipeline.sources.protocol.DataSourceAdapter`
to the engine: parse the source format into the FHIR IR, run
``process_data_batch`` (the same rule engine + validation barrier FHIR uses),
then serialize back to the source format.

This is the seam that lets HL7 v2 / CDA / DICOM go through the engine (NLP,
gPAS, scoring, the output gate) instead of a fixed blanket scrubber.
"""

from __future__ import annotations

from pipeline.config.service import get_settings
from pipeline.processor import process_data_batch
from pipeline.validation import enforce_output


def run_through_engine(
    adapter,
    raw: bytes | str,
    config_profile: str = "auto",
) -> bytes:
    """Parse *raw* with *adapter*, de-identify via the engine, serialize back.

    Args:
        adapter:        a DataSourceAdapter instance (single-use per object).
        raw:            the source-format bytes/text.
        config_profile: which rule profile to apply (same profiles as FHIR).

    Returns:
        De-identified bytes in the adapter's source format.

    Raises:
        NormalizationError: if *raw* is malformed for the adapter's format.
        OutputBlocked: if the unified output validation barrier blocks the result.
    """
    output, _ = run_through_engine_with_manifest(adapter, raw, config_profile)
    return output


def run_through_engine_with_manifest(
    adapter,
    raw: bytes | str,
    config_profile: str = "auto",
) -> tuple[bytes, list[dict]]:
    """Like :func:`run_through_engine` but also returns the transformation manifest.

    The manifest is the flattened list of rule-engine manifest entries
    (``{rule, match, action, ...}``) across the mapped resources — the same
    transformation record FHIR produces, on the fields mapped from the source
    format. The FHIR ``meta.tag`` manifest is stripped from the resources before
    serialising, so the released native output never carries it inline.
    """
    from pipeline.manifest import strip_manifest_tag

    settings = get_settings(config_profile)
    resources = adapter.parse(raw)
    # pseudonymizer=None lets the engine resolve its default (gPAS) lazily.
    results, manifests = process_data_batch(
        resources,
        settings,
        pseudonymizer=None,
        attach_manifest=True,
        _return_manifest=True,
    )
    enforce_output(results, config_profile=config_profile)

    manifest: list[dict] = []
    for entries in manifests or []:
        for entry in entries or []:
            manifest.append(entry)
    for r in results:
        if isinstance(r, dict):
            strip_manifest_tag(r)
    return adapter.serialize(results), manifest
