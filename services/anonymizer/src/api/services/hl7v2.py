"""HL7 v2 processing service."""

import asyncio
import logging

from formats.hl7v2 import deidentify_hl7v2, deidentify_hl7v2_batch

logger = logging.getLogger("medanon")


def _engine_deidentify_hl7v2(message_text: str, config_profile: str) -> str:
    """Run one HL7 v2 message through the full rule engine via the adapter.

    Sync (runs in a worker thread).  Maps PID demographics to the FHIR IR,
    applies the configured profile (redact / generalize / gPAS / NLP / scoring
    + the validation barrier), and writes the result back into the message.
    """
    from pipeline.sources import Hl7v2Adapter
    from pipeline.sources.run import run_through_engine

    out = run_through_engine(Hl7v2Adapter(), message_text, config_profile)
    return out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else out


class Hl7v2Service:
    """Runs HL7 v2 de-identification in a thread pool to keep the event loop free."""

    async def process_single(
        self, message_text: str, config_profile: str | None = None
    ) -> str:
        """De-identify a single HL7 v2 message.

        When *config_profile* is provided the message is routed through the full
        rule engine (the same one FHIR uses) via ``Hl7v2Adapter``.  When it is
        ``None`` the legacy fixed-field scrubber is used (backward-compatible
        default).

        Raises:
            ValueError / NormalizationError: invalid HL7 v2 message.
            PiiLeakError: output blocked by the validation barrier.
        """
        if config_profile:
            return await asyncio.to_thread(
                _engine_deidentify_hl7v2, message_text, config_profile
            )
        return await asyncio.to_thread(deidentify_hl7v2, message_text)

    async def process_batch(self, batch_text: str) -> str:
        """De-identify a batch of concatenated HL7 v2 messages.

        Raises:
            ValueError: If any individual message within the batch is invalid.
        """
        return await asyncio.to_thread(deidentify_hl7v2_batch, batch_text)
