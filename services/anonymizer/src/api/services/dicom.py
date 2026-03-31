"""DICOM processing service."""
import asyncio
import io
import logging
import zipfile

from pipeline.dicom_deidentify import deidentify_dicom

logger = logging.getLogger("medanon")


class DicomService:
    async def process_single(self, raw: bytes) -> bytes:
        """De-identify a single DICOM file. Returns de-identified DICOM bytes."""
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
