"""CDA (HL7 v3) processing service.

CDA documents are de-identified through the full rule engine via ``CdaAdapter``:
patient demographics (recordTarget) are mapped to the FHIR IR, the configured
profile is applied (redact / generalize / gPAS / NLP / scoring + the validation
barrier), and the de-identified values are written back into the original XML.

Unlike HL7 v2 / DICOM there is no legacy blanket-scrubber endpoint for CDA — the
``formats/cda.py`` scrubber exists but was never wired to a route — so this
service always uses the engine path.
"""

import asyncio
import logging

logger = logging.getLogger("medanon")


def _engine_deidentify_cda(xml_text: str, config_profile: str) -> str:
    """Run one CDA document through the rule engine via the adapter (sync)."""
    from pipeline.sources import CdaAdapter
    from pipeline.sources.run import run_through_engine

    out = run_through_engine(CdaAdapter(), xml_text, config_profile)
    return out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else out


class CdaService:
    """Runs CDA de-identification in a worker thread to keep the loop free."""

    async def process_single(self, xml_text: str, config_profile: str = "auto") -> str:
        """De-identify a single CDA document.

        Raises:
            NormalizationError: if *xml_text* is not a valid CDA document.
            PiiLeakError: if the validation barrier blocks output.
        """
        return await asyncio.to_thread(_engine_deidentify_cda, xml_text, config_profile)
