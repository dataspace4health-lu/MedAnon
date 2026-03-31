"""DICOM de-identification — PS3.15 Annex E Basic Application Level Confidentiality Profile.

Scrubs all Type 1 and Type 2 identifying attributes defined in the DICOM
PS 3.15 Table E.1-1.  Pixel data is not modified (pixel-burned annotations
require an OCR pass that is outside the scope of this module).

Usage:
    from pipeline.dicom_deidentify import deidentify_dicom
    output_bytes = deidentify_dicom(raw_bytes)
"""

import io

# ---------------------------------------------------------------------------
# PS3.15 Annex E Table E.1-1 — Basic Application Level Confidentiality Profile
# All (group, element) tags that must be removed or zeroed under the Basic Profile.
# ---------------------------------------------------------------------------

DICOM_PS315_TAGS: frozenset[tuple[int, int]] = frozenset(
    [
        # --- Patient identification ---
        (0x0010, 0x0010),  # PatientName
        (0x0010, 0x0020),  # PatientID
        (0x0010, 0x0021),  # IssuerOfPatientID
        (0x0010, 0x0030),  # PatientBirthDate
        (0x0010, 0x0040),  # PatientSex
        (0x0010, 0x0050),  # PatientInsurancePlanCodeSequence
        (0x0010, 0x1000),  # OtherPatientIDs
        (0x0010, 0x1001),  # OtherPatientNames
        (0x0010, 0x1005),  # PatientBirthName
        (0x0010, 0x1010),  # PatientAge
        (0x0010, 0x1020),  # PatientSize
        (0x0010, 0x1030),  # PatientWeight
        (0x0010, 0x1040),  # PatientAddress
        (0x0010, 0x1090),  # MedicalRecordLocator
        (0x0010, 0x2000),  # MedicalAlerts
        (0x0010, 0x2110),  # Allergies
        (0x0010, 0x2154),  # PatientTelephoneNumbers
        (0x0010, 0x2160),  # EthnicGroup
        (0x0010, 0x2180),  # Occupation
        (0x0010, 0x21B0),  # AdditionalPatientHistory
        (0x0010, 0x21D0),  # LastMenstrualDate
        (0x0010, 0x21F0),  # PatientReligiousPreference
        (0x0010, 0x4000),  # PatientComments
        # --- Study / series / acquisition identifiers ---
        (0x0008, 0x0014),  # InstanceCreatorUID
        (0x0008, 0x0020),  # StudyDate
        (0x0008, 0x0021),  # SeriesDate
        (0x0008, 0x0022),  # AcquisitionDate
        (0x0008, 0x0023),  # ContentDate
        (0x0008, 0x0024),  # OverlayDate
        (0x0008, 0x0025),  # CurveDate
        (0x0008, 0x002A),  # AcquisitionDateTime
        (0x0008, 0x0030),  # StudyTime
        (0x0008, 0x0031),  # SeriesTime
        (0x0008, 0x0032),  # AcquisitionTime
        (0x0008, 0x0033),  # ContentTime
        (0x0008, 0x0050),  # AccessionNumber
        (0x0008, 0x0080),  # InstitutionName
        (0x0008, 0x0081),  # InstitutionAddress
        (0x0008, 0x0082),  # InstitutionCodeSequence
        (0x0008, 0x0090),  # ReferringPhysicianName
        (0x0008, 0x0092),  # ReferringPhysicianAddress
        (0x0008, 0x0094),  # ReferringPhysicianTelephoneNumbers
        (0x0008, 0x009C),  # ConsultingPhysicianName
        (0x0008, 0x009D),  # ConsultingPhysicianIdentificationSequence
        (0x0008, 0x103E),  # SeriesDescription
        (0x0008, 0x1048),  # PhysiciansOfRecord
        (0x0008, 0x1049),  # PhysiciansOfRecordIdentificationSequence
        (0x0008, 0x1060),  # NameOfPhysiciansReadingStudy
        (0x0008, 0x1062),  # PhysiciansReadingStudyIdentificationSequence
        (0x0008, 0x1070),  # OperatorsName
        (0x0008, 0x1072),  # OperatorsIdentificationSequence
        (0x0008, 0x1080),  # AdmittingDiagnosesDescription
        (0x0008, 0x1084),  # AdmittingDiagnosesCodeSequence
        (0x0008, 0x1110),  # ReferencedStudySequence
        (0x0008, 0x1111),  # ReferencedPerformedProcedureStepSequence
        (0x0008, 0x1115),  # ReferencedSeriesSequence
        (0x0008, 0x1195),  # TransactionUID
        (0x0008, 0x1250),  # RelatedSeriesSequence
        (0x0008, 0x2111),  # DerivationDescription
        (0x0008, 0x3010),  # IrradiationEventUID
        (0x0008, 0x4000),  # IdentifyingComments
        # --- Equipment / device ---
        (0x0018, 0x1000),  # DeviceSerialNumber
        (0x0018, 0x1002),  # DeviceUID
        (0x0018, 0x1030),  # ProtocolName
        (0x0018, 0x700A),  # DetectorID
        # --- Study / series UIDs ---
        (0x0020, 0x000D),  # StudyInstanceUID
        (0x0020, 0x000E),  # SeriesInstanceUID
        (0x0020, 0x0200),  # SynchronizationFrameOfReferenceUID
        (0x0020, 0x9161),  # ConcatenationUID
        (0x0020, 0x9164),  # DimensionOrganizationUID
        # --- Admission / visit ---
        (0x0038, 0x0010),  # AdmissionID
        (0x0038, 0x0020),  # AdmittingDate
        (0x0038, 0x0021),  # AdmittingTime
        (0x0038, 0x0300),  # CurrentPatientLocation
        (0x0038, 0x0400),  # PatientInstitutionResidence
        (0x0038, 0x0500),  # PatientState
        # --- Scheduled procedure step ---
        (0x0040, 0x0006),  # ScheduledPerformingPhysicianName
        (0x0040, 0x0009),  # ScheduledProcedureStepID
        (0x0040, 0x0010),  # ScheduledStationName
        (0x0040, 0x0241),  # PerformedStationAETitle
        (0x0040, 0x0242),  # PerformedStationName
        (0x0040, 0x0243),  # PerformedLocation
        (0x0040, 0x0244),  # PerformedProcedureStepStartDate
        (0x0040, 0x0245),  # PerformedProcedureStepStartTime
        (0x0040, 0x0250),  # PerformedProcedureStepEndDate
        (0x0040, 0x0251),  # PerformedProcedureStepEndTime
        (0x0040, 0x0253),  # PerformedProcedureStepID
        (0x0040, 0x0254),  # PerformedProcedureStepDescription
        (0x0040, 0x0275),  # RequestAttributesSequence
        (0x0040, 0x1004),  # PatientTransportArrangements
        (0x0040, 0x1400),  # RequestedProcedureComments
        (0x0040, 0xA07C),  # CustodialOrganizationSequence
        (0x0040, 0xA124),  # UID
        (0x0040, 0xDB0D),  # TemplateVersion
        # --- Presentation / overlay ---
        (0x0070, 0x0001),  # TextObjectSequence
        (0x0070, 0x0084),  # ContentCreatorName
        (0x0070, 0x0086),  # ContentCreatorIdentificationCodeSequence
        # --- Storage ---
        (0x0088, 0x0140),  # StorageMediaFileSetUID
        # --- Multi-frame functional groups ---
        (0x5200, 0x9229),  # SharedFunctionalGroupsSequence
        # --- Digital signatures / original attributes ---
        (0x0400, 0x0561),  # OriginalAttributesSequence
    ]
)


def _walk_and_scrub(dataset) -> None:  # type: ignore[type-arg]
    """Recursively remove all DICOM_PS315_TAGS from *dataset*.

    For SQ (Sequence) data elements each contained item is visited recursively
    before the sequence tag itself is evaluated — this ensures nested UIDs and
    identifying attributes inside sequences are scrubbed even when the parent
    sequence tag is not on the removal list.

    Args:
        dataset: a ``pydicom.Dataset`` (or compatible mapping) to scrub in place.
    """
    tags_to_delete: list = []

    for elem in dataset:
        tag = (elem.tag.group, elem.tag.element)

        if tag in DICOM_PS315_TAGS:
            tags_to_delete.append(elem.tag)
            # No need to recurse into a sequence we are about to delete.
            continue

        # Recurse into sequences that are *not* themselves scheduled for removal.
        if elem.VR == "SQ":
            for item in elem.value:
                _walk_and_scrub(item)

    for tag in tags_to_delete:
        del dataset[tag]


def deidentify_dicom(raw_bytes: bytes) -> bytes:
    """De-identify a DICOM object according to PS3.15 Annex E Basic Profile.

    Removes all identifying attributes listed in ``DICOM_PS315_TAGS``, then
    stamps the dataset with the mandatory de-identification markers:

    - ``(0012,0062)`` PatientIdentityRemoved = ``"YES"``
    - ``(0012,0063)`` DeidentificationMethod = ``"PS3.15 Annex E Basic Profile"``

    Pixel Data (7FE0,0010) is intentionally preserved — pixel-burned text
    annotations require a separate OCR-based scrubbing pass.

    Args:
        raw_bytes: Raw bytes of a DICOM Part 10 file (with or without preamble).

    Returns:
        De-identified DICOM bytes.

    Raises:
        ValueError: If *raw_bytes* cannot be parsed as a valid DICOM object.
    """
    import pydicom
    import pydicom.errors

    try:
        ds = pydicom.dcmread(io.BytesIO(raw_bytes), force=True)
    except pydicom.errors.InvalidDicomError as exc:
        raise ValueError("Invalid DICOM data") from exc

    _walk_and_scrub(ds)

    # Stamp mandatory de-identification markers (PS3.15 §E.3.1)
    patient_identity_removed = pydicom.DataElement(
        tag=(0x0012, 0x0062),
        VR="CS",
        value="YES",
    )
    ds[0x0012, 0x0062] = patient_identity_removed

    deidentification_method = pydicom.DataElement(
        tag=(0x0012, 0x0063),
        VR="LO",
        value="PS3.15 Annex E Basic Profile",
    )
    ds[0x0012, 0x0063] = deidentification_method

    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds)
    return buf.getvalue()
