"""DICOM processing service."""

import asyncio
import io
import logging
import zipfile

from formats.dicom import deidentify_dicom

logger = logging.getLogger("medanon")


def _engine_deidentify_dicom(raw: bytes, config_profile: str) -> bytes:
    """Run one DICOM object through the full rule engine via the adapter.

    Sync (runs in a worker thread).  Maps patient/study tags to the FHIR IR,
    applies the configured profile + validation barrier, writes back by tag,
    and stamps the PS3.15 §E.3.1 de-identification markers.
    """
    from pipeline.sources import DicomAdapter
    from pipeline.sources.run import run_through_engine

    return run_through_engine(DicomAdapter(), raw, config_profile)


class DicomService:
    async def process_single(
        self, raw: bytes, config_profile: str | None = None
    ) -> bytes:
        """De-identify a single DICOM file. Returns de-identified DICOM bytes.

        When *config_profile* is provided the object is routed through the full
        rule engine via ``DicomAdapter``; otherwise the legacy PS3.15 blanket
        scrubber is used (backward-compatible default).
        """
        if config_profile:
            return await asyncio.to_thread(
                _engine_deidentify_dicom, raw, config_profile
            )
        return await asyncio.to_thread(deidentify_dicom, raw)

    async def process_batch(self, files: list[tuple[str, bytes]]) -> bytes:
        """De-identify multiple DICOM files. Returns a ZIP archive as bytes.

        Args:
            files: list of (filename, dicom_bytes) tuples

        Returns:
            ZIP archive bytes containing de-identified DICOM files.
            Files that fail processing are replaced with a ``<filename>.error.txt``
            marker so the rest of the batch is not aborted.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for filename, raw in files:
                try:
                    deidentified = await asyncio.to_thread(deidentify_dicom, raw)
                    zf.writestr(filename, deidentified)
                except Exception as exc:
                    logger.warning(
                        "dicom_batch: failed to process %s: %s",
                        filename,
                        exc,
                    )
                    # Write an error marker file instead of failing the whole batch.
                    zf.writestr(f"{filename}.error.txt", f"Processing failed: {exc}")
        return buf.getvalue()
