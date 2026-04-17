"""Presidio engine singleton and custom PatternRecognizer definitions.

Provides the process-level AnalyzerEngine (lazy-initialized, thread-safe)
and the full catalogue of custom recognizers extending Presidio's built-ins
to cover HIPAA Safe Harbor, GDPR Art. 9, and EU national identifiers.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("medanon.nlp")

# ---------------------------------------------------------------------------
# Entity catalogue
# ---------------------------------------------------------------------------

HEALTHCARE_ENTITIES = [
    # --- Presidio built-in recognizers ---
    "PERSON",
    "DATE_TIME",
    "LOCATION",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "MEDICAL_LICENSE",
    "AGE",
    "NRP",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_BANK_NUMBER",
    "US_ITIN",
    "IP_ADDRESS",
    "URL",
    # --- Custom recognizers (registered in _build_custom_recognizers) ---
    "STREET_ADDRESS",
    "INTL_PHONE",
    "FAX_NUMBER",
    # EU national identifiers
    "UK_NINO",
    "FR_NIR",
    "DE_SVNR",
    "NL_BSN",
    "IT_CF",
    "ES_DNI",
    "CH_AHV",
    "BE_NN",
    # Healthcare IDs
    "MRN",
    "UK_NHS",
    "DE_KVNR",
    "EU_EHIC",
    # Postcodes
    "UK_POSTCODE",
    "EU_POSTCODE",
    # Labeled markers
    "DOB_MARKER",
    "AGE_MARKER",
    "NATIONAL_ID_LABEL",
    "ACCOUNT_LABEL",
    "LICENSE_PLATE",
    # GDPR Art.9 special categories (labeled)
    "NATIONALITY_LABEL",
    "RELIGION_LABEL",
    "POLITICAL_LABEL",
    "ETHNICITY_LABEL",
    # Date formats not covered by built-in DATE_TIME
    "EU_DATE",
    "EU_DATE_WRITTEN",
    # Synthetic data artifacts
    "SYNTHEA_SEED",
]

# ---------------------------------------------------------------------------
# Presidio engine singleton (lazy, thread-safe)
# ---------------------------------------------------------------------------

_ENGINE_LOCK = threading.Lock()
_ANALYZER = None


def _build_custom_recognizers():
    """Build PatternRecognizer instances for PII types not covered by Presidio builtins."""
    from presidio_analyzer import Pattern, PatternRecognizer

    recognizers = []

    def _add(entity, name, patterns, context=None):
        recognizers.append(PatternRecognizer(
            supported_entity=entity,
            name=name,
            patterns=[Pattern(name=n, regex=r, score=s) for n, r, s in patterns],
            supported_language="en",
            context=context or [],
        ))

    # --- Addresses ---
    _add("STREET_ADDRESS", "us_address_recognizer", [
        ("us_address", (
            r"\b\d{1,6}\s+"
            r"(?:[A-Z][a-z''\-]*\.?\s+){1,4}"
            r"(?:Street|St\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
            r"Road|Rd\.?|Lane|Ln\.?|Way|Court|Ct\.?|Place|Pl\.?|Circle|Cir\.?|"
            r"Trail|Trl\.?|Terrace|Ter\.?|Parkway|Pkwy\.?|Highway|Hwy\.?|"
            r"Alley|Approach|Bay|Brook|Burg|Bypass|Byway|"
            r"Causeway|Center|Common|Commons|Corner|Corners|Course|"
            r"Cove|Creek|Crossing|Dam|Divide|Estate|Estates|"
            r"Expressway|Extension|Falls|Ferry|Field|Fields|Flat|Flats|"
            r"Ford|Forge|Fork|Forks|Freeway|Garden|Gardens|Gateway|Glen|"
            r"Green|Grove|Harbor|Haven|Heights|Hollow|"
            r"Isle|Junction|Key|Knoll|Lake|Landing|Light|Loaf|Lock|Lodge|Loop|"
            r"Mall|Manor|Meadow|Meadows|Mill|Mission|Mount|Neck|"
            r"Orchard|Oval|Overpass|Parade|Park|Pass|Path|Pike|Pine|"
            r"Plain|Plains|Plaza|Point|Port|Prairie|Promenade|"
            r"Ramp|Ranch|Rapid|Rapids|Rest|Ridge|River|Route|Row|Run|"
            r"Shore|Spring|Springs|Spur|Square|Station|Stravenue|Stream|Summit|"
            r"Trace|Track|Trafficway|Tunnel|Turnpike|Union|Valley|Viaduct|View|Village|"
            r"Vista|Walk|Well|Wells)"
            r"(?:\s+(?:Apt|Apartment|Unit|Suite|Ste|Bldg|Building|Floor|Fl|Rm|Room|#)\.?\s*\d{1,5})?"
            r"(?:[,\s]+(?:[A-Z][a-z''\-]+\s*)+)?"
            r"(?:[,\s]+[A-Z]{2})?"
            r"(?:[,\s]+\d{5}(?:-\d{4})?)?"
        ), 0.85),
    ])

    _add("STREET_ADDRESS", "eu_address_recognizer", [
        ("de_address", (
            r"\b(?:[A-ZÄÖÜ][a-zäöüß]+(?:straße|strasse|str\.?|gasse|weg|platz|allee|ring|damm|ufer))"
            r"\s+\d{1,5}(?:\s?[a-zA-Z])?"
        ), 0.85),
        ("fr_address", (
            r"\b(?:(?:Rue|Avenue|Boulevard|Bd\.?|Chemin|Place|Allée|Impasse|Passage|Quai)"
            r"(?:\s+(?:de|du|des|la|le|l')?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
            r"\s+\d{1,5}"
        ), 0.85),
        ("it_address", (
            r"\b(?:(?:Via|Viale|Piazza|Corso|Largo|Vicolo)"
            r"(?:\s+(?:dei?|del|della|delle|degli)?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
            r"\s*,?\s*\d{1,5}"
        ), 0.85),
        ("es_address", (
            r"\b(?:(?:Calle|Avenida|Avda\.?|Paseo|Plaza|Carrera|Camino)"
            r"(?:\s+(?:de|del|la|las|los)?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
            r"\s*,?\s*\d{1,5}"
        ), 0.85),
        ("nl_address", (
            r"\b(?:[A-Z][a-z]+(?:straat|laan|weg|gracht|plein|singel|kade|dijk))"
            r"\s+\d{1,5}(?:\s?[a-zA-Z])?"
        ), 0.85),
        ("po_box", (
            r"\b(?:P\.?O\.?\s*Box|Postfach|Boîte\s+Postale|BP|Casella\s+Postale|CP|"
            r"Apartado(?:\s+de\s+Correos)?|Postbus)\s*[:#]?\s*\d{1,10}\b"
        ), 0.85),
    ])

    # --- Phone / Fax ---
    _add("INTL_PHONE", "intl_phone_recognizer", [
        ("intl_phone", (
            r"(?<!\d)(?:\+|00)\d{1,3}"
            r"(?:[.\-\s]?\(?\d{1,5}\)?)+"
            r"(?:[.\-\s]?\d{2,5}){1,4}(?!\d)"
        ), 0.6),
    ])

    _add("FAX_NUMBER", "fax_recognizer", [
        ("fax", (
            r"\b(?:fax|telefax|facsimile)\s*[:#\-]?\s*"
            r"(?:\+|00)?[\d\s.\-()]{7,20}"
        ), 0.85),
    ], context=["fax", "telefax", "facsimile"])

    # --- EU National Identifiers ---
    _add("UK_NINO", "uk_nino_recognizer", [
        ("nino", (
            r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)"
            r"[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]"
            r"\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"
        ), 0.85),
    ], context=["national insurance", "NI number", "NINO"])

    _add("FR_NIR", "fr_nir_recognizer", [
        ("nir", r"\b[12]\s?\d{2}\s?\d{2}\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}\b", 0.7),
    ], context=["NIR", "sécurité sociale", "sécu"])

    _add("DE_SVNR", "de_svnr_recognizer", [
        ("svnr", r"\b\d{2}\s?\d{6}\s?[A-Z]\s?\d{3}\b", 0.7),
    ], context=["Sozialversicherung", "SVNR", "Versicherungsnummer"])

    _add("NL_BSN", "nl_bsn_recognizer", [
        ("bsn_labeled", (
            r"\b(?:BSN|burgerservicenummer)\s*[:#\-]?\s*\d{9}\b"
        ), 0.85),
    ], context=["BSN", "burgerservicenummer"])

    _add("IT_CF", "it_cf_recognizer", [
        ("codice_fiscale", r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", 0.9),
    ], context=["codice fiscale", "CF"])

    _add("ES_DNI", "es_dni_recognizer", [
        ("dni", r"\b(?:\d{8}[A-Z]|[XYZ]\d{7}[A-Z])\b", 0.85),
    ], context=["DNI", "NIE", "documento"])

    _add("CH_AHV", "ch_ahv_recognizer", [
        ("ahv", r"\b756\.\d{4}\.\d{4}\.\d{2}\b", 0.95),
    ])

    _add("BE_NN", "be_nn_recognizer", [
        ("nn_be", r"\b\d{2}\.\d{2}\.\d{2}-\d{3}\.\d{2}\b", 0.85),
    ], context=["rijksregister", "national number"])

    # --- Healthcare IDs ---
    _add("MRN", "mrn_recognizer", [
        ("mrn", (
            r"\b(?:"
            r"MRN|MR|medical\s+record(?:\s+number)?|"
            r"patient\s*(?:id|number|no\.?|#|identifier)|"
            r"hospital\s*(?:id|number|no\.?|#)|"
            r"case\s*(?:id|number|no\.?|#)|"
            r"encounter\s*(?:id|number|no\.?)|"
            r"visit\s*(?:id|number|no\.?)|"
            r"chart\s*(?:id|number|no\.?)|"
            r"Fallnummer|Fall-?Nr\.?|Patientennummer|Pat\.?\s*Nr\.?|Patientennr\.?|"
            r"numéro\s+de\s+dossier|n°?\s*dossier|IPP|NDA|"
            r"número\s+de\s+historia|NHC|"
            r"patiëntnummer|dossiernummer"
            r")[\s:#\-]*[A-Z0-9\-]{2,30}\b"
        ), 0.85),
    ], context=["MRN", "medical record", "patient"])

    _add("UK_NHS", "uk_nhs_recognizer", [
        ("nhs", (
            r"\b(?:NHS(?:\s+number)?[:\s\-]*)?(?<!\d)\d{3}\s?\d{3}\s?\d{4}(?!\d)\b"
        ), 0.5),
    ], context=["NHS", "NHS number"])

    _add("DE_KVNR", "de_kvnr_recognizer", [
        ("kvnr", r"\b[A-Z]\d{9}\b", 0.5),
    ], context=["Krankenversichertennummer", "KVNR", "Versichertennummer"])

    _add("EU_EHIC", "eu_ehic_recognizer", [
        ("ehic", (
            r"\b(?:EHIC|European\s+Health\s+Insurance|CEAM|TEAM|Carte\s+Européenne|"
            r"Gesundheitskarte|tessera\s+sanitaria)"
            r"(?:\s+(?:card|number|no\.?|nr\.?|#))?"
            r"\s*[:#\-]?\s*[A-Z0-9\s\-]{8,80}\b"
        ), 0.85),
    ], context=["EHIC", "health insurance card"])

    # --- Postcodes ---
    _add("UK_POSTCODE", "uk_postcode_recognizer", [
        ("uk_postcode", r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", 0.6),
    ], context=["postcode", "post code", "zip"])

    _add("EU_POSTCODE", "eu_postcode_recognizer", [
        ("nl_postcode", r"\b\d{4}\s?[A-Z]{2}\b", 0.6),
        ("pl_postcode", r"\b\d{2}-\d{3}\b", 0.5),
        ("se_postcode", r"\b\d{3}\s\d{2}\b", 0.5),
    ], context=["postcode", "PLZ", "code postal", "código postal"])

    # --- Labeled Markers ---
    _add("DOB_MARKER", "dob_marker_recognizer", [
        ("dob", (
            r"\b(?:DOB|D\.O\.B\.?|date\s+of\s+birth|born(?:\s+on)?|birth\s*date|"
            r"Geburtsdatum|date\s+de\s+naissance|fecha\s+de\s+nacimiento|"
            r"data\s+di\s+nascita|geboortedatum)"
            r"[:\s\-]+\d{1,4}[\.\-/]\d{1,2}[\.\-/]\d{2,4}\b"
        ), 0.95),
    ], context=["DOB", "date of birth", "born"])

    _add("AGE_MARKER", "age_marker_recognizer", [
        ("age_labeled", (
            r"\b(?:age|aged|Alter|âge|edad|età)\s*[:\-]?\s*\d{1,3}\b"
        ), 0.85),
        ("age_years_old", (
            r"\b\d{1,3}\s*(?:-\s*)?(?:year|yr)s?\s*(?:-\s*)?old\b"
        ), 0.85),
        ("age_eu_lang", (
            r"\b\d{1,3}\s*(?:Jahre?\s+alt|ans|años|anni)\b"
        ), 0.85),
    ], context=["age", "years old"])

    _add("NATIONAL_ID_LABEL", "national_id_label_recognizer", [
        ("national_id", (
            r"\b(?:national\s*id|nid|passport(?:\s*number)?|id(?:\s*number)?|"
            r"identity\s*card|carte\s+d'identité|personalausweis|"
            r"documento\s+de\s+identidad|carta\s+d'identità)\s*"
            r"[:#-]?\s*[A-Z0-9\-]{4,50}\b"
        ), 0.85),
    ], context=["national ID", "passport", "identity"])

    _add("ACCOUNT_LABEL", "account_label_recognizer", [
        ("account", (
            r"\b(?:account|acct|insurance|member|policy|Versicherung|"
            r"assurance|verzekering|seguro|assicurazione)\s*"
            r"(?:id|number|no\.?|#|nummer|numéro|número)?\s*[:#-]?\s*[A-Z0-9\-]{4,50}\b"
        ), 0.7),
    ], context=["account", "insurance", "policy"])

    _add("LICENSE_PLATE", "license_plate_recognizer", [
        ("plate", (
            r"\b(?:license\s*plate|registration|plate|Kennzeichen|immatriculation|"
            r"matrícula|targa|kenteken)\s*[:#\-]?\s*[A-Z0-9\-\s]{3,12}\b"
        ), 0.85),
    ], context=["license plate", "registration", "vehicle"])

    # --- GDPR Art.9 Special Categories ---
    _add("NATIONALITY_LABEL", "nationality_recognizer", [
        ("nationality", (
            r"\b(?:nationality|citizenship|Staatsangehörigkeit|nationalité|"
            r"nacionalidad|nazionalità|nationaliteit)\s*[:\-]\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,40}\b"
        ), 0.85),
    ])

    _add("RELIGION_LABEL", "religion_recognizer", [
        ("religion", (
            r"\b(?:religion|faith|confession|Konfession|Glaube|"
            r"religión|religione|religie|croyance)\s*[:\-]\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,40}\b"
        ), 0.85),
    ])

    _add("POLITICAL_LABEL", "political_recognizer", [
        ("political", (
            r"\b(?:political(?:\s+opinion|\s+affiliation)?|party|"
            r"politische\s+(?:Meinung|Überzeugung)|parti\s+politique|"
            r"afiliación\s+política)\s*[:\-]\s*"
            r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,80}\b"
        ), 0.85),
    ])

    _add("ETHNICITY_LABEL", "ethnicity_recognizer", [
        ("ethnicity", (
            r"\b(?:ethnic(?:ity)?|ethnische\s+Herkunft|race|racial\s+origin|"
            r"origine\s+(?:ethnique|razziale)|origen\s+étnico|etniciteit)"
            r"\s*[:\-]\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,40}\b"
        ), 0.85),
    ])

    # --- EU Date Formats ---
    _add("EU_DATE", "eu_date_recognizer", [
        ("date_eu", r"\b\d{1,2}[.\-]\d{1,2}[.\-]\d{2,4}\b", 0.4),
    ])

    _add("EU_DATE_WRITTEN", "eu_date_written_recognizer", [
        ("date_written_eu", (
            r"\b\d{1,2}\.?\s*(?:"
            r"Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember|"
            r"janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre|"
            r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|"
            r"gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre|"
            r"januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|"
            r"janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
            r")\s+\d{4}\b"
        ), 0.85),
    ])

    # --- Synthetic Data Artifacts ---
    _add("SYNTHEA_SEED", "synthea_seed_recognizer", [
        ("synthea_seed", r"\b(?:Person|Population)\s+seed\s*:\s*-?\d{5,}\b", 0.95),
    ])

    return recognizers


def _get_analyzer():
    """Return the process-level Presidio AnalyzerEngine, initializing once."""
    global _ANALYZER
    if _ANALYZER is not None:
        return _ANALYZER
    with _ENGINE_LOCK:
        if _ANALYZER is not None:
            return _ANALYZER
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            log.info("Initializing Presidio AnalyzerEngine with en_core_web_lg …")
            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
                    "ner_model_configuration": {
                        "labels_to_ignore": [
                            "CARDINAL",
                            "ORDINAL",
                            "QUANTITY",
                            "PERCENT",
                            "MONEY",
                            "PRODUCT",
                            "WORK_OF_ART",
                            "LAW",
                            "LANGUAGE",
                            "FAC",
                            "EVENT",
                        ],
                    },
                }
            )
            _ANALYZER = AnalyzerEngine(nlp_engine=provider.create_engine())
            custom = _build_custom_recognizers()
            for rec in custom:
                _ANALYZER.registry.add_recognizer(rec)
            log.info(
                "Presidio ready — %d entity types, %d custom recognizers",
                len(HEALTHCARE_ENTITIES),
                len(custom),
            )
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize Presidio / spaCy.  "
                "Ensure these packages are installed: "
                "presidio-analyzer, spacy, en_core_web_lg.  "
                f"Original error: {exc}"
            ) from exc
    return _ANALYZER
