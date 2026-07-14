"""gPAS FHIR Parameters builders and response parsers (pure data transformation, no I/O).

Four functions covering the two FHIR operations used by the gPAS integration:
  $pseudonymize[AllowCreate]  get-or-create pseudonym for original values
  $dePseudonymize             reverse lookup: pseudonym -> original value
"""


def _build_pseudonymize_params(domain, original_values):
    """Build a FHIR Parameters request for $pseudonymize[AllowCreate].

    Args:
        domain: gPAS domain name (string, e.g. "MIRACUM")
        original_values: list of original string values to pseudonymize
    """
    params_list = [{"name": "target", "valueString": domain}]
    for val in original_values:
        params_list.append({"name": "original", "valueString": str(val)})
    return {
        "resourceType": "Parameters",
        "parameter": params_list,
    }


def _build_depseudonymize_params(domain, pseudonym_values):
    """Build a FHIR Parameters request for $dePseudonymize.

    Args:
        domain: gPAS domain name
        pseudonym_values: list of pseudonym strings to reverse-lookup
    """
    params_list = [{"name": "target", "valueString": domain}]
    for val in pseudonym_values:
        params_list.append({"name": "pseudonym", "valueString": str(val)})
    return {
        "resourceType": "Parameters",
        "parameter": params_list,
    }


def _parse_pseudonymize_response(resp_json):
    """Parse a $pseudonymize[AllowCreate] response into a mapping dict.

    Returns:
        dict mapping original_value -> pseudonym_value
    Raises:
        ValueError for any ``error`` entries in the response.
    """
    mapping = {}
    errors = []
    for param in resp_json.get("parameter", []):
        name = param.get("name")
        parts = {p["name"]: p for p in param.get("part", [])}
        if name == "pseudonym":
            orig = parts.get("original", {}).get("valueIdentifier", {}).get("value")
            psn = parts.get("pseudonym", {}).get("valueIdentifier", {}).get("value")
            if orig is not None and psn is not None:
                mapping[orig] = psn
        elif name == "error":
            orig_id = (
                parts.get("original", {}).get("valueIdentifier", {}).get("value", "?")
            )
            code = (
                parts.get("error-code", {})
                .get("valueCoding", {})
                .get("code", "unknown")
            )
            errors.append(f"original={orig_id} error={code}")
    if errors:
        raise ValueError(f"gPAS returned errors: {'; '.join(errors)}")
    return mapping


def _parse_depseudonymize_response(resp_json):
    """Parse a $dePseudonymize response into a mapping dict.

    Returns:
        dict mapping pseudonym_value -> original_value
    """
    mapping = {}
    errors = []
    for param in resp_json.get("parameter", []):
        name = param.get("name")
        parts = {p["name"]: p for p in param.get("part", [])}
        if name == "original":
            psn = parts.get("pseudonym", {}).get("valueIdentifier", {}).get("value")
            orig = parts.get("original", {}).get("valueIdentifier", {}).get("value")
            if psn is not None and orig is not None:
                mapping[psn] = orig
        elif name == "error":
            psn_id = (
                parts.get("pseudonym", {}).get("valueIdentifier", {}).get("value", "?")
            )
            code = (
                parts.get("error-code", {})
                .get("valueCoding", {})
                .get("code", "unknown")
            )
            errors.append(f"pseudonym={psn_id} error={code}")
    if errors:
        raise ValueError(f"gPAS returned errors: {'; '.join(errors)}")
    return mapping
