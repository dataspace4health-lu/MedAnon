"""DICOM processing service."""

import asyncio
import io
import logging
import zipfile

from formats.dicom import deidentify_dicom, deidentify_dicom_with_manifest

logger = logging.getLogger("medanon")


def _engine_deidentify_dicom(raw: bytes, config_profile: str) -> tuple[bytes, list]:
    """Run one DICOM object through the rule engine, returning (bytes, manifest)."""
    from pipeline.sources import DicomAdapter
    from pipeline.sources.run import run_through_engine_with_manifest

    return run_through_engine_with_manifest(DicomAdapter(), raw, config_profile)


class DicomService:
    async def process_single(
        self, raw: bytes, config_profile: str | None = None
    ) -> bytes:
        """De-identify a single DICOM file (output only)."""
        out, _ = await self.process_single_with_manifest(raw, config_profile)
        return out

    async def process_single_with_manifest(
        self, raw: bytes, config_profile: str | None = None
    ) -> tuple[bytes, list]:
        """De-identify one DICOM file, returning (bytes, transformation manifest).

        With *config_profile* the object routes through the full rule engine via
        ``DicomAdapter``; otherwise the legacy PS3.15 blanket scrubber is used.
        Both return a manifest of the tags removed/set.
        """
        if config_profile:
            return await asyncio.to_thread(
                _engine_deidentify_dicom, raw, config_profile
            )
        return await asyncio.to_thread(deidentify_dicom_with_manifest, raw)

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
