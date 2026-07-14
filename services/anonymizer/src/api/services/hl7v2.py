"""HL7 v2 processing service."""

import asyncio
import logging

from formats.hl7v2 import (
    deidentify_hl7v2_batch,
    deidentify_hl7v2_with_manifest,
)

logger = logging.getLogger("medanon")


def _engine_deidentify_hl7v2(
    message_text: str, config_profile: str
) -> tuple[str, list]:
    """Run one HL7 v2 message through the rule engine, returning (msg, manifest)."""
    from pipeline.sources import Hl7v2Adapter
    from pipeline.sources.run import run_through_engine_with_manifest

    out, manifest = run_through_engine_with_manifest(
        Hl7v2Adapter(), message_text, config_profile
    )
    msg = out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else out
    return msg, manifest


class Hl7v2Service:
    """Runs HL7 v2 de-identification in a thread pool to keep the event loop free."""

    async def process_single(
        self, message_text: str, config_profile: str | None = None
    ) -> str:
        """De-identify a single HL7 v2 message (output only)."""
        out, _ = await self.process_single_with_manifest(message_text, config_profile)
        return out

    async def process_single_with_manifest(
        self, message_text: str, config_profile: str | None = None
    ) -> tuple[str, list]:
        """De-identify one HL7 v2 message, returning (message, transformation manifest).

        With *config_profile* the message routes through the full rule engine (the
        same one FHIR uses) via ``Hl7v2Adapter``; without it the legacy fixed-field
        scrubber is used. Both return a manifest of the fields transformed.

        Raises:
            ValueError / NormalizationError: invalid HL7 v2 message.
            PiiLeakError: output blocked by the validation barrier.
        """
        if config_profile:
            return await asyncio.to_thread(
                _engine_deidentify_hl7v2, message_text, config_profile
            )
        return await asyncio.to_thread(deidentify_hl7v2_with_manifest, message_text)

    async def process_batch(self, batch_text: str) -> str:
        """De-identify a batch of concatenated HL7 v2 messages.

        Raises:
            ValueError: If any individual message within the batch is invalid.
        """
        return await asyncio.to_thread(deidentify_hl7v2_batch, batch_text)
