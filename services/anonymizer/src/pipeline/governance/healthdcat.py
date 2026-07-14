"""HealthDCAT-AP dataset-discovery descriptor (TEHDAS2 D7.2 §4.3, EHDS Art 55/78).

The EHDS requires health datasets to be described for discovery through the EU
Health Data Catalogue. HealthDCAT-AP is the health extension of DCAT-AP for that
purpose. This module emits a **JSON-LD** ``dcat:Dataset`` descriptor from
caller-supplied descriptive metadata, enriched with the privacy/quality facts a
Transformation Passport already carries (record count, achieved k/l/t,
anonymisation vs pseudonymisation, DP parameters, retention).

Output is discovery metadata only  no PHI, no record-level content. It is a
dependency-free dict (stdlib) so it slots into the JSON API surface directly; a
consumer can serialise it to Turtle/RDF with any JSON-LD processor.

The ``healthdcatap:`` prefix tracks the SEMIC HealthDCAT-AP draft namespace; it
is centralised in ``_CONTEXT`` so it can be updated as the specification settles.
"""

from __future__ import annotations

from typing import Any

_CONTEXT: dict[str, Any] = {
    "dcat": "http://www.w3.org/ns/dcat#",
    "dct": "http://purl.org/dc/terms/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "dpv": "https://w3id.org/dpv#",
    "dqv": "http://www.w3.org/ns/dqv#",
    "healthdcatap": "http://healthdataportal.eu/ns/health#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}


def _publisher(name: str | None) -> dict | None:
    if not name:
        return None
    return {"@type": "foaf:Agent", "foaf:name": name}


def _passport_enrichment(passport: dict) -> dict[str, Any]:
    """Derive HealthDCAT-AP / DQV fields from a Transformation Passport."""
    out: dict[str, Any] = {}
    orig = passport.get("original_dataset") or {}
    processing = passport.get("processing") or {}
    pm = processing.get("privacy_model") or {}
    gen = processing.get("generalization") or {}

    released = orig.get("released")
    if released is None:
        released = orig.get("total_resources")
    if released is not None:
        out["healthdcatap:numberOfRecords"] = int(released)

    # De-identification technique + formal privacy parameters as provenance.
    quality_notes: list[dict[str, Any]] = []
    achieved_k = gen.get("achieved_k")
    if achieved_k is not None:
        quality_notes.append(
            {"dqv:isMeasurementOf": "k-anonymity", "dqv:value": int(achieved_k)}
        )
    if gen.get("achieved_l") is not None:
        quality_notes.append(
            {"dqv:isMeasurementOf": "l-diversity", "dqv:value": gen.get("achieved_l")}
        )
    if pm.get("target_t") is not None:
        quality_notes.append(
            {"dqv:isMeasurementOf": "t-closeness", "dqv:value": pm.get("target_t")}
        )
    if quality_notes:
        out["dqv:hasQualityMeasurement"] = quality_notes

    dp = processing.get("differential_privacy")
    if dp:
        spent = (dp.get("spent") or {}) if isinstance(dp, dict) else {}
        eps = spent.get("epsilon", dp.get("epsilon") if isinstance(dp, dict) else None)
        if eps is not None:
            out["healthdcatap:privacyEpsilon"] = eps

    # Anonymised vs pseudonymised: a disclosure decision implies an anonymisation
    # target; a permit binding implies pseudonymisation under a legal basis.
    if passport.get("disclosure"):
        out["dpv:hasProcessing"] = "dpv:Anonymise"
    elif (passport.get("identification") or {}).get("permit_id"):
        out["dpv:hasProcessing"] = "dpv:Pseudonymise"

    return out


def build_healthdcat_descriptor(
    *,
    dataset: dict,
    passport: dict | None = None,
) -> dict[str, Any]:
    """Build a HealthDCAT-AP ``dcat:Dataset`` descriptor (JSON-LD).

    Args:
        dataset: descriptive metadata. Recognised keys: ``id``, ``title``,
            ``description``, ``publisher``, ``license``, ``access_rights``,
            ``themes`` (list), ``keywords`` (list), ``health_themes`` (list),
            ``temporal`` (``{"start", "end"}``), ``spatial``, ``language`` (list),
            ``contact_point``, ``retention_period``, ``legal_basis``.
        passport: optional Transformation Passport whose privacy/quality facts are
            folded into the descriptor.

    Returns:
        JSON-LD dict with ``@context`` + ``@type`` ``dcat:Dataset``.
    """
    if not dataset.get("title"):
        raise ValueError("dataset.title is required for a discoverable descriptor")

    node: dict[str, Any] = {
        "@context": dict(_CONTEXT),
        "@type": "dcat:Dataset",
        "dct:title": dataset["title"],
    }
    if dataset.get("id"):
        node["@id"] = str(dataset["id"])
        node["dct:identifier"] = str(dataset["id"])
    if dataset.get("description"):
        node["dct:description"] = dataset["description"]

    publisher = _publisher(dataset.get("publisher"))
    if publisher:
        node["dct:publisher"] = publisher
    if dataset.get("license"):
        node["dct:license"] = dataset["license"]
    if dataset.get("access_rights"):
        node["dct:accessRights"] = dataset["access_rights"]
    if dataset.get("contact_point"):
        node["dcat:contactPoint"] = dataset["contact_point"]

    if dataset.get("themes"):
        node["dcat:theme"] = list(dataset["themes"])
    if dataset.get("keywords"):
        node["dcat:keyword"] = list(dataset["keywords"])
    if dataset.get("health_themes"):
        node["healthdcatap:healthTheme"] = list(dataset["health_themes"])
    if dataset.get("language"):
        node["dct:language"] = list(dataset["language"])
    if dataset.get("spatial"):
        node["dct:spatial"] = dataset["spatial"]

    temporal = dataset.get("temporal") or {}
    if temporal.get("start") or temporal.get("end"):
        node["dct:temporal"] = {
            "@type": "dct:PeriodOfTime",
            **({"dcat:startDate": temporal["start"]} if temporal.get("start") else {}),
            **({"dcat:endDate": temporal["end"]} if temporal.get("end") else {}),
        }

    if dataset.get("retention_period"):
        node["healthdcatap:retentionPeriod"] = dataset["retention_period"]
    if dataset.get("legal_basis"):
        node["dpv:hasLegalBasis"] = dataset["legal_basis"]

    if passport:
        node.update(_passport_enrichment(passport))

    return node
