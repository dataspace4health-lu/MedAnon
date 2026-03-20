"""HAPI FHIR REST helpers."""
import os
import requests

FHIR_URL = os.environ.get("FHIR_URL", "http://localhost:8081/fhir").rstrip("/")
TIMEOUT = 15


def _get(path: str, params: dict = None) -> tuple[bool, dict]:
    try:
        r = requests.get(
            f"{FHIR_URL}{path}",
            params=params,
            headers={"Accept": "application/fhir+json"},
            timeout=TIMEOUT,
        )
        return r.status_code == 200, r.json()
    except Exception as e:
        return False, {"error": str(e)}


def capability_statement() -> tuple[bool, dict]:
    return _get("/metadata")


def patient_count() -> int:
    ok, data = _get("/Patient", {"_summary": "count"})
    if ok:
        return data.get("total", 0)
    return 0


def search_patients(name: str = "", count: int = 20) -> list[dict]:
    params = {"_count": count, "_elements": "id,name,birthDate,gender"}
    if name:
        params["name"] = name
    ok, bundle = _get("/Patient", params)
    if not ok:
        return []
    rows = []
    for entry in bundle.get("entry", []):
        r = entry.get("resource", {})
        names = r.get("name", [{}])
        display_name = " ".join(
            names[0].get("given", []) + [names[0].get("family", "")]
        ).strip() if names else ""
        rows.append({
            "id": r.get("id", ""),
            "name": display_name or "(no name)",
            "birthDate": r.get("birthDate", ""),
            "gender": r.get("gender", ""),
        })
    return rows


def get_patient(patient_id: str) -> tuple[bool, dict]:
    return _get(f"/Patient/{patient_id}")


def search_conditions(query: str = "", clinical_status: str = "", count: int = 50) -> list[dict]:
    """Search Condition resources by free-text illness name or SNOMED code.

    Uses HAPI's code:text search; _include=Condition:subject fetches linked
    Patient resources in the same Bundle response.
    """
    params: dict = {"_count": count, "_include": "Condition:subject"}
    if query.strip():
        if query.strip().isdigit():
            params["code"] = f"http://snomed.info/sct|{query.strip()}"
        else:
            params["code:text"] = query.strip()
    if clinical_status and clinical_status != "any":
        params["clinical-status"] = clinical_status

    ok, bundle = _get("/Condition", params)
    if not ok:
        return []

    patients_by_id: dict[str, dict] = {}
    conditions: list[dict] = []

    for entry in bundle.get("entry", []):
        r = entry.get("resource", {})
        rtype = r.get("resourceType", "")

        if rtype == "Patient":
            pid = r.get("id", "")
            names = r.get("name", [{}])
            display_name = " ".join(
                names[0].get("given", []) + [names[0].get("family", "")]
            ).strip() if names else ""
            patients_by_id[pid] = {
                "name": display_name or "(no name)",
                "birthDate": r.get("birthDate", ""),
                "gender": r.get("gender", ""),
            }

        elif rtype == "Condition":
            coding = r.get("code", {}).get("coding", [{}])
            first_coding = coding[0] if coding else {}
            code_text = (
                r.get("code", {}).get("text")
                or first_coding.get("display", "")
            )
            ref = (
                r.get("subject", {}).get("reference", "")
                or r.get("patient", {}).get("reference", "")
            )
            patient_id = ref.split("/")[-1] if "/" in ref else ref
            conditions.append({
                "condition_id": r.get("id", ""),
                "code": first_coding.get("code", ""),
                "display": code_text,
                "clinical_status": (
                    r.get("clinicalStatus", {})
                    .get("coding", [{}])[0]
                    .get("code", "")
                    if isinstance(r.get("clinicalStatus"), dict)
                    else ""
                ),
                "patient_id": patient_id,
            })

    rows = []
    for c in conditions:
        pat = patients_by_id.get(c["patient_id"], {})
        rows.append({**c, **{
            "patient_name": pat.get("name", "(unknown)"),
            "patient_birth_date": pat.get("birthDate", ""),
            "patient_gender": pat.get("gender", ""),
        }})
    return rows
