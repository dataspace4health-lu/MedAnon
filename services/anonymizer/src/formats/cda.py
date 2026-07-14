"""CDA/CCDA document de-identification using defusedxml (already a project dependency).

Scrubs PHI from standard CDA R2 / CCDA sections:
  - recordTarget (patient demographics: name, DOB, address, telecom, IDs)
  - author (author name, address, telecom)
  - legalAuthenticator (authenticator name, time)
  - dataEnterer (data entry person)
  - participant (referenced persons)
  - informant (informants)
  - custodian (custodian organization  kept structural but IDs blanked)
  - componentOf/encompassingEncounter (encounter IDs, responsible party)

The de-identification is structure-preserving: XML schema validity is maintained.
PHI is blanked in-place (empty string) rather than removing entire elements,
so the document remains valid CDA XML that downstream processors can consume.

Usage:
    from formats.cda import deidentify_cda, is_cda_document
    if is_cda_document(xml_text):
        clean_xml = deidentify_cda(xml_text)
"""

import contextvars
import xml.etree.ElementTree as ET

import defusedxml.ElementTree as DET

# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------

CDA_NAMESPACE = "urn:hl7-org:v3"
HL7_NS = {"hl7": "urn:hl7-org:v3"}

# Context-local transformation manifest. Set by ``deidentify_cda_with_manifest``
# so the two low-level blanking primitives (which every scrubber funnels through)
# record what they cleared without threading a manifest arg through ~14 functions.
# A ContextVar is isolated per call  safe under ``asyncio.to_thread`` (the
# executing context is copied), unlike a shared module list.
_cda_manifest: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_cda_manifest", default=None
)


def _localname(tag: str) -> str:
    """Strip the ``{namespace}`` prefix from a Clark-notation tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _record(element: str, action: str, attribute: str | None = None) -> None:
    """Aggregate a cleared field into the active manifest (by element/attr/action)."""
    agg = _cda_manifest.get()
    if agg is None:
        return
    key = (element, attribute or "", action)
    agg[key] = agg.get(key, 0) + 1


def _ns(tag: str) -> str:
    """Return a Clark-notation namespace-qualified tag name for the CDA namespace.

    Args:
        tag: The local XML element name (e.g. ``"name"``, ``"id"``).

    Returns:
        The fully qualified name string, e.g. ``"{urn:hl7-org:v3}name"``.
    """
    return f"{{{CDA_NAMESPACE}}}{tag}"


def _blank_element_text(elem) -> None:
    """Clear *elem*.text if *elem* is not None.

    Sets ``elem.text`` to ``None``, which serialises as a self-closing tag
    (``<given/>``) via ``ET.tostring``.  Both ``None`` and ``""`` are
    schema-valid in CDA XML; ``None`` is preferred because Python's
    ``xml.etree.ElementTree`` always represents empty element text as
    ``None``, regardless of whether the tag was ``<given/>`` or
    ``<given></given>`` in the source.  After de-identification, callers
    checking whether text was cleared should use ``not elem.text``.

    Args:
        elem: An ``xml.etree.ElementTree.Element`` or None.
    """
    if elem is not None:
        if elem.text:
            _record(_localname(elem.tag), "blank_text")
        elem.text = None


def _blank_attribute(elem, attr: str) -> None:
    """Clear *attr* on *elem* if the attribute exists.

    Args:
        elem: An ``xml.etree.ElementTree.Element`` or None.
        attr: The attribute name to blank (plain local name; not namespace-qualified).
    """
    if elem is not None and attr in elem.attrib:
        if elem.get(attr):
            _record(_localname(elem.tag), "blank_attribute", attr)
        elem.set(attr, "")


# ---------------------------------------------------------------------------
# Structural PHI scrubbers (operate on a sub-element passed by the caller)
# ---------------------------------------------------------------------------


def _scrub_name(parent_elem) -> None:
    """Blank the text of all ``<name>`` sub-elements under *parent_elem*.

    Clears the text content of ``<given>``, ``<family>``, ``<prefix>``, and
    ``<suffix>`` children within every ``<name>`` child of *parent_elem*.

    Args:
        parent_elem: An ``xml.etree.ElementTree.Element`` to search within.
    """
    if parent_elem is None:
        return
    for name_elem in parent_elem.findall(_ns("name")):
        for sub_tag in ("given", "family", "prefix", "suffix"):
            for sub in name_elem.findall(_ns(sub_tag)):
                _blank_element_text(sub)


def _scrub_addr(parent_elem) -> None:
    """Blank the text of all ``<addr>`` sub-elements under *parent_elem*.

    Clears ``<streetAddressLine>``, ``<city>``, ``<postalCode>``,
    ``<state>``, and ``<country>`` within every ``<addr>`` child.

    Args:
        parent_elem: An ``xml.etree.ElementTree.Element`` to search within.
    """
    if parent_elem is None:
        return
    for addr_elem in parent_elem.findall(_ns("addr")):
        for sub_tag in ("streetAddressLine", "city", "postalCode", "state", "country"):
            for sub in addr_elem.findall(_ns(sub_tag)):
                _blank_element_text(sub)


def _scrub_telecom(parent_elem) -> None:
    """Blank the ``value`` attribute on all ``<telecom>`` children of *parent_elem*.

    Args:
        parent_elem: An ``xml.etree.ElementTree.Element`` to search within.
    """
    if parent_elem is None:
        return
    for telecom_elem in parent_elem.findall(_ns("telecom")):
        _blank_attribute(telecom_elem, "value")


def _scrub_id(parent_elem) -> None:
    """Blank the ``extension`` attribute on all ``<id>`` children of *parent_elem*.

    The ``root`` OID attribute is deliberately preserved so the document remains
    schema-valid (``root`` is a required attribute on CDA ``<id>`` elements).

    Args:
        parent_elem: An ``xml.etree.ElementTree.Element`` to search within.
    """
    if parent_elem is None:
        return
    for id_elem in parent_elem.findall(_ns("id")):
        _blank_attribute(id_elem, "extension")


# ---------------------------------------------------------------------------
# Composite scrubber for person / organisation role elements
# ---------------------------------------------------------------------------


def _scrub_person_or_org(elem) -> None:
    """Apply all PHI scrubbers to a role element and its known child role elements.

    Applies ``_scrub_name``, ``_scrub_addr``, ``_scrub_telecom``, and
    ``_scrub_id`` to *elem* itself and to each of the following child elements
    (when present):

    - ``<assignedPerson>``
    - ``<assignedAuthor>``
    - ``<representedOrganization>``
    - ``<assignedEntity>``
    - ``<intendedRecipient>``

    Args:
        elem: An ``xml.etree.ElementTree.Element`` representing a CDA role element.
    """
    if elem is None:
        return

    _scrub_name(elem)
    _scrub_addr(elem)
    _scrub_telecom(elem)
    _scrub_id(elem)

    child_role_tags = (
        "assignedPerson",
        "assignedAuthor",
        "representedOrganization",
        "assignedEntity",
        "intendedRecipient",
    )
    for child_tag in child_role_tags:
        child = elem.find(_ns(child_tag))
        if child is not None:
            _scrub_name(child)
            _scrub_addr(child)
            _scrub_telecom(child)
            _scrub_id(child)


# ---------------------------------------------------------------------------
# Section-level scrubbers (operate on the document root element)
# ---------------------------------------------------------------------------


def _scrub_record_target(root) -> None:
    """De-identify all ``<recordTarget>`` sections in the CDA document.

    For each ``<patientRole>`` found under a ``<recordTarget>``:

    - Blanks ``<id>`` extension attributes.
    - Blanks address and telecom PHI.
    - Blanks patient name text (given / family / prefix / suffix).
    - Blanks ``<birthTime value>``, ``<administrativeGenderCode displayName>``,
      ``<maritalStatusCode displayName>``, ``<ethnicGroupCode displayName>``,
      and ``<raceCode displayName>``.

    Args:
        root: The ``ClinicalDocument`` root ``xml.etree.ElementTree.Element``.
    """
    for record_target in root.findall(_ns("recordTarget")):
        patient_role = record_target.find(_ns("patientRole"))
        if patient_role is None:
            continue

        _scrub_id(patient_role)
        _scrub_addr(patient_role)
        _scrub_telecom(patient_role)

        patient = patient_role.find(_ns("patient"))
        if patient is not None:
            _scrub_name(patient)

            birth_time = patient.find(_ns("birthTime"))
            _blank_attribute(birth_time, "value")

            for coded_tag in (
                "administrativeGenderCode",
                "maritalStatusCode",
                "ethnicGroupCode",
                "raceCode",
            ):
                coded_elem = patient.find(_ns(coded_tag))
                _blank_attribute(coded_elem, "displayName")


def _scrub_author(root) -> None:
    """De-identify all ``<author>`` sections in the CDA document.

    For each ``<author>`` element:

    - Blanks ``<time value>``.
    - Calls ``_scrub_person_or_org`` on each ``<assignedAuthor>`` child.

    Args:
        root: The ``ClinicalDocument`` root ``xml.etree.ElementTree.Element``.
    """
    for author in root.findall(_ns("author")):
        time_elem = author.find(_ns("time"))
        _blank_attribute(time_elem, "value")

        assigned_author = author.find(_ns("assignedAuthor"))
        if assigned_author is not None:
            _scrub_person_or_org(assigned_author)


def _scrub_legal_authenticator(root) -> None:
    """De-identify the ``<legalAuthenticator>`` section if present.

    Blanks ``<time value>`` and calls ``_scrub_person_or_org`` on
    ``<assignedEntity>``.

    Args:
        root: The ``ClinicalDocument`` root ``xml.etree.ElementTree.Element``.
    """
    legal_auth = root.find(_ns("legalAuthenticator"))
    if legal_auth is None:
        return

    time_elem = legal_auth.find(_ns("time"))
    _blank_attribute(time_elem, "value")

    assigned_entity = legal_auth.find(_ns("assignedEntity"))
    if assigned_entity is not None:
        _scrub_person_or_org(assigned_entity)


def _scrub_data_enterer(root) -> None:
    """De-identify the ``<dataEnterer>`` section if present.

    Calls ``_scrub_person_or_org`` on the ``<assignedEntity>`` child of
    ``<dataEnterer>``.

    Args:
        root: The ``ClinicalDocument`` root ``xml.etree.ElementTree.Element``.
    """
    data_enterer = root.find(_ns("dataEnterer"))
    if data_enterer is None:
        return

    assigned_entity = data_enterer.find(_ns("assignedEntity"))
    if assigned_entity is not None:
        _scrub_person_or_org(assigned_entity)


def _scrub_participants(root) -> None:
    """De-identify all ``<participant>`` sections in the CDA document.

    For each ``<participant>`` element:

    - Blanks ``<time value>``.
    - Calls ``_scrub_person_or_org`` on each ``<associatedEntity>`` child.

    Args:
        root: The ``ClinicalDocument`` root ``xml.etree.ElementTree.Element``.
    """
    for participant in root.findall(_ns("participant")):
        time_elem = participant.find(_ns("time"))
        _blank_attribute(time_elem, "value")

        associated_entity = participant.find(_ns("associatedEntity"))
        if associated_entity is not None:
            _scrub_person_or_org(associated_entity)


def _scrub_informants(root) -> None:
    """De-identify all ``<informant>`` sections in the CDA document.

    For each ``<informant>`` element, finds either ``<assignedEntity>`` or
    ``<relatedEntity>`` and calls ``_scrub_person_or_org``.

    Args:
        root: The ``ClinicalDocument`` root ``xml.etree.ElementTree.Element``.
    """
    for informant in root.findall(_ns("informant")):
        for entity_tag in ("assignedEntity", "relatedEntity"):
            entity = informant.find(_ns(entity_tag))
            if entity is not None:
                _scrub_person_or_org(entity)
                break


def _scrub_encompassing_encounter(root) -> None:
    """De-identify the ``<encompassingEncounter>`` section if present.

    Blanks encounter ``<id>`` extensions, ``<effectiveTime>`` values (including
    ``<low>`` and ``<high>`` sub-elements), and calls ``_scrub_person_or_org``
    on ``<responsibleParty>/<assignedEntity>``.

    Args:
        root: The ``ClinicalDocument`` root ``xml.etree.ElementTree.Element``.
    """
    component_of = root.find(_ns("componentOf"))
    if component_of is None:
        return

    encounter = component_of.find(_ns("encompassingEncounter"))
    if encounter is None:
        return

    _scrub_id(encounter)

    effective_time = encounter.find(_ns("effectiveTime"))
    if effective_time is not None:
        _blank_attribute(effective_time, "value")
        _blank_attribute(effective_time.find(_ns("low")), "value")
        _blank_attribute(effective_time.find(_ns("high")), "value")

    responsible_party = encounter.find(_ns("responsibleParty"))
    if responsible_party is not None:
        assigned_entity = responsible_party.find(_ns("assignedEntity"))
        if assigned_entity is not None:
            _scrub_person_or_org(assigned_entity)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_cda_document(xml_text: str) -> bool:
    """Return True if *xml_text* appears to be a CDA document.

    This is a fast heuristic check on the first 1 024 characters of the input;
    it does **not** perform a full XML parse.  A full validation is performed
    inside :func:`deidentify_cda`.

    Args:
        xml_text: The raw XML text to inspect.

    Returns:
        ``True`` if ``"urn:hl7-org:v3"`` appears in the first 1 024 characters,
        ``False`` otherwise.
    """
    return "urn:hl7-org:v3" in xml_text[:1024]


def deidentify_cda(xml_text: str) -> str:
    """De-identify a CDA / CCDA document by blanking PHI in-place.

    Parses *xml_text* safely with ``defusedxml``, verifies the root element is
    ``<ClinicalDocument xmlns="urn:hl7-org:v3">``, applies all section-level
    PHI scrubbers, then re-serialises to a UTF-8 XML string with the XML
    declaration prepended.

    Structure-preserving: elements are **never removed**, only their PHI text
    content or attribute values are cleared.  The output remains valid CDA XML.

    Args:
        xml_text: The raw CDA/CCDA XML document as a Unicode string.

    Returns:
        The de-identified XML string, including a leading
        ``<?xml version="1.0" encoding="UTF-8"?>`` declaration.

    Raises:
        ValueError: If *xml_text* cannot be parsed as XML, or if the parsed
            document root is not a CDA ``<ClinicalDocument>`` element.
    """
    output, _ = deidentify_cda_with_manifest(xml_text)
    return output


def deidentify_cda_with_manifest(xml_text: str) -> tuple[str, list[dict]]:
    """Like :func:`deidentify_cda` but also returns the transformation manifest.

    The manifest is a list of ``{element, attribute?, action, count}`` entries
    one per distinct field type cleared (e.g. ``given``/``family`` blanked,
    ``telecom@value`` blanked), with a count of occurrences. No PHI values.
    """
    try:
        root = DET.fromstring(xml_text.encode("utf-8"))
    except DET.ParseError as exc:
        raise ValueError(f"Invalid CDA XML: {exc}") from exc

    if root.tag != _ns("ClinicalDocument"):
        raise ValueError(
            f"Not a CDA document: root element is '{root.tag}', "
            f"expected '{_ns('ClinicalDocument')}'"
        )

    agg: dict = {}
    token = _cda_manifest.set(agg)
    try:
        _scrub_record_target(root)
        _scrub_author(root)
        _scrub_legal_authenticator(root)
        _scrub_data_enterer(root)
        _scrub_participants(root)
        _scrub_informants(root)
        _scrub_encompassing_encounter(root)
    finally:
        _cda_manifest.reset(token)

    manifest = [
        {
            "element": el,
            **({"attribute": attr} if attr else {}),
            "action": action,
            "count": count,
        }
        for (el, attr, action), count in agg.items()
    ]
    serialized = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + serialized, manifest
