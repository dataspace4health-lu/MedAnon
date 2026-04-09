"""HL7 v2 processing service."""

import asyncio
import logging

from formats.hl7v2 import deidentify_hl7v2, deidentify_hl7v2_batch

logger = logging.getLogger("medanon")


class Hl7v2Service:
    """Runs HL7 v2 de-identification in a thread pool to keep the event loop free."""

    async def process_single(self, message_text: str) -> str:
        """De-identify a single HL7 v2 message.

        Raises:
            ValueError: If *message_text* is not a valid HL7 v2 message.
        """
        return await asyncio.to_thread(deidentify_hl7v2, message_text)

    async def process_batch(self, batch_text: str) -> str:
        """De-identify a batch of concatenated HL7 v2 messages.

        Raises:
            ValueError: If any individual message within the batch is invalid.
        """
        return await asyncio.to_thread(deidentify_hl7v2_batch, batch_text)
