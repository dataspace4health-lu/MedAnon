"""Tests for CDA/CCDA document de-identification.

All tests run locally without Docker or external services.
"""

import xml.etree.ElementTree as ET

import pytest

from formats.cda import CDA_NAMESPACE, deidentify_cda, is_cda_document

# ---------------------------------------------------------------------------
# Test fixture
# ---------------------------------------------------------------------------

MINIMAL_CDA = '''<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <realmCode code="US"/>
  <typeId root="2.16.840.1.113883.1.3" extension="POCD_HD000040"/>
  <id root="2.16.840.1.113883.19.5.99999.1" extension="TT988"/>
  <code code="34133-9" displayName="Summarization of Episode Note" codeSystem="2.16.840.1.113883.6.1" codeSystemName="LOINC"/>
  <title>Continuity of Care Document</title>
  <effectiveTime value="20230101120000"/>
  <confidentialityCode code="N" codeSystem="2.16.840.1.113883.5.25"/>
  <languageCode code="en-US"/>
  <recordTarget>
    <patientRole>
      <id root="2.16.840.1.113883.19.5.99999.2" extension="MRN12345"/>
      <addr>
        <streetAddressLine>123 Main Street</streetAddressLine>
        <city>Springfield</city>
        <state>IL</state>
        <postalCode>62701</postalCode>
      </addr>
      <telecom value="tel:+15555551234" use="HP"/>
      <patient>
        <name>
          <given>John</given>
          <family>Smith</family>
        </name>
        <birthTime value="19800101"/>
        <administrativeGenderCode code="M" displayName="Male"/>
        <raceCode code="2106-3" displayName="White"/>
      </patient>
    </patientRole>
  </recordTarget>
  <author>
    <time value="20230101120000"/>
    <assignedAuthor>
      <id root="2.16.840.1.113883.19.5" extension="DR12345"/>
      <addr>
        <streetAddressLine>456 Clinic Road</streetAddressLine>
        <city>Springfield</city>
      </addr>
      <telecom value="tel:+15555559876" use="WP"/>
      <assignedPerson>
        <name>
          <given>Alice</given>
          <family>Johnson</family>
        </name>
      </assignedPerson>
    </assignedAuthor>
  </author>
  <legalAuthenticator>
    <time value="20230101120000"/>
    <signatureCode code="S"/>
    <assignedEntity>
      <id root="2.16.840.1.113883.19.5" extension="AUTH456"/>
      <assignedPerson>
        <name>
          <given>Bob</given>
          <family>Williams</family>
        </name>
      </assignedPerson>
    </assignedEntity>
  </legalAuthenticator>
  <component>
    <structuredBody/>
  </component>
</ClinicalDocument>'''

NS = f"{{{CDA_NAMESPACE}}}"


def _parse(xml_text: str) -> ET.Element:
    """Parse the de-identified output for assertion helpers."""
    return ET.fromstring(xml_text)


# ---------------------------------------------------------------------------
# is_cda_document
# ---------------------------------------------------------------------------


def test_is_cda_document_true():
    assert is_cda_document(MINIMAL_CDA) is True


def test_is_cda_document_false_for_fhir():
    fhir_json = '{"resourceType": "Patient", "id": "example"}'
    assert is_cda_document(fhir_json) is False


def test_is_cda_document_false_for_fhir_xml():
    fhir_xml = '<Patient xmlns="http://hl7.org/fhir"><id value="example"/></Patient>'
    assert is_cda_document(fhir_xml) is False


# ---------------------------------------------------------------------------
# Patient de-identification
# ---------------------------------------------------------------------------


def test_patient_name_blanked():
    result = deidentify_cda(MINIMAL_CDA)
    root = _parse(result)

    patient_role = root.find(f"{NS}recordTarget/{NS}patientRole")
    assert patient_role is not None

    patient = patient_role.find(f"{NS}patient")
    assert patient is not None

    name = patient.find(f"{NS}name")
    assert name is not None

    given = name.find(f"{NS}given")
    family = name.find(f"{NS}family")
    assert given is not None and not given.text
    assert family is not None and not family.text


def test_patient_mrn_blanked():
    result = deidentify_cda(MINIMAL_CDA)
    root = _parse(result)

    patient_role = root.find(f"{NS}recordTarget/{NS}patientRole")
    assert patient_role is not None

    id_elem = patient_role.find(f"{NS}id")
    assert id_elem is not None
    # root OID is preserved; extension is blanked
    assert id_elem.get("root") == "2.16.840.1.113883.19.5.99999.2"
    assert id_elem.get("extension") == ""


def test_patient_address_blanked():
    result = deidentify_cda(MINIMAL_CDA)
    root = _parse(result)

    patient_role = root.find(f"{NS}recordTarget/{NS}patientRole")
    assert patient_role is not None

    addr = patient_role.find(f"{NS}addr")
    assert addr is not None

    street = addr.find(f"{NS}streetAddressLine")
    city = addr.find(f"{NS}city")
    state = addr.find(f"{NS}state")
    postal = addr.find(f"{NS}postalCode")

    assert street is not None and not street.text
    assert city is not None and not city.text
    assert state is not None and not state.text
    assert postal is not None and not postal.text


def test_patient_telecom_blanked():
    result = deidentify_cda(MINIMAL_CDA)
    root = _parse(result)

    patient_role = root.find(f"{NS}recordTarget/{NS}patientRole")
    assert patient_role is not None

    telecom = patient_role.find(f"{NS}telecom")
    assert telecom is not None
    assert telecom.get("value") == ""
    # use attribute (non-PHI) should be untouched
    assert telecom.get("use") == "HP"


# ---------------------------------------------------------------------------
# Author de-identification
# ---------------------------------------------------------------------------


def test_author_name_blanked():
    result = deidentify_cda(MINIMAL_CDA)
    root = _parse(result)

    assigned_author = root.find(f"{NS}author/{NS}assignedAuthor")
    assert assigned_author is not None

    assigned_person = assigned_author.find(f"{NS}assignedPerson")
    assert assigned_person is not None

    name = assigned_person.find(f"{NS}name")
    assert name is not None

    given = name.find(f"{NS}given")
    family = name.find(f"{NS}family")
    assert given is not None and not given.text
    assert family is not None and not family.text


def test_author_id_blanked():
    result = deidentify_cda(MINIMAL_CDA)
    root = _parse(result)

    assigned_author = root.find(f"{NS}author/{NS}assignedAuthor")
    assert assigned_author is not None

    id_elem = assigned_author.find(f"{NS}id")
    assert id_elem is not None
    assert id_elem.get("root") == "2.16.840.1.113883.19.5"
    assert id_elem.get("extension") == ""


# ---------------------------------------------------------------------------
# Legal authenticator de-identification
# ---------------------------------------------------------------------------


def test_legal_authenticator_name_blanked():
    result = deidentify_cda(MINIMAL_CDA)
    root = _parse(result)

    legal_auth = root.find(f"{NS}legalAuthenticator")
    assert legal_auth is not None

    assigned_entity = legal_auth.find(f"{NS}assignedEntity")
    assert assigned_entity is not None

    assigned_person = assigned_entity.find(f"{NS}assignedPerson")
    assert assigned_person is not None

    name = assigned_person.find(f"{NS}name")
    assert name is not None

    given = name.find(f"{NS}given")
    family = name.find(f"{NS}family")
    assert given is not None and not given.text
    assert family is not None and not family.text


# ---------------------------------------------------------------------------
# Document integrity
# ---------------------------------------------------------------------------


def test_cda_structure_preserved():
    result = deidentify_cda(MINIMAL_CDA)

    # Must be parseable XML
    root = _parse(result)

    # Root must be ClinicalDocument in the HL7 v3 namespace
    assert root.tag == f"{NS}ClinicalDocument"

    # Non-PHI structural elements must be intact
    title = root.find(f"{NS}title")
    assert title is not None and title.text == "Continuity of Care Document"

    realm_code = root.find(f"{NS}realmCode")
    assert realm_code is not None and realm_code.get("code") == "US"


def test_xml_declaration_preserved():
    result = deidentify_cda(MINIMAL_CDA)
    assert result.startswith("<?xml")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_malformed_xml_raises_value_error():
    truncated = MINIMAL_CDA[:200]  # cuts off mid-document
    with pytest.raises(ValueError, match="Invalid CDA XML"):
        deidentify_cda(truncated)


def test_non_cda_xml_raises_value_error():
    non_cda = '<?xml version="1.0"?><root><child/></root>'
    with pytest.raises(ValueError, match="Not a CDA document"):
        deidentify_cda(non_cda)


def test_xml_bomb_blocked():
    """defusedxml must block entity expansion (XXE / billion-laughs attacks)."""
    xxe_payload = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<ClinicalDocument xmlns="urn:hl7-org:v3">'
        "<title>&xxe;</title>"
        "</ClinicalDocument>"
    )
    # defusedxml raises DTDForbidden (a subclass of its own ParseError, not
    # xml.etree.ElementTree.ParseError) — we accept any exception from the
    # defusedxml family to keep the assertion robust across versions.
    import defusedxml

    with pytest.raises((defusedxml.DTDForbidden, ValueError)):
        deidentify_cda(xxe_payload)
