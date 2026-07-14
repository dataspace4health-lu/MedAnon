"""Presidio engine singleton and custom PatternRecognizer definitions.

Provides the process-level AnalyzerEngine (lazy-initialized, thread-safe)
and the full catalogue of custom recognizers extending Presidio's built-ins
to cover HIPAA Safe Harbor, GDPR Art. 9, and EU national identifiers.

IMPORTANT — ENTITY CATALOGUE SYNC:
    HEALTHCARE_ENTITIES here must stay in sync with the canonical copy in
    services/anonymizer/src/integrations/nlp/utils.py.
    Any addition here MUST also be added there, and vice-versa.
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
    # Clinical note PHI — inline demographic / administrative
    "GENDER",  # inline patient gender (male/female/non-binary etc.)
    "RACE_ETHNICITY",  # bare inline race/ethnicity (not label-prefixed)
    "INSURANCE_STATUS",  # coverage status, named payers
    "GEO_COORDINATES",  # decimal/DMS lat-long pairs
    # Organizations (hospitals, clinics, payers) — relies on spaCy NER
    "ORGANIZATION",
    # Financial / fiscal identifiers
    "SWIFT_BIC",
    "EU_VAT",
    "US_ROUTING",
    "CRYPTO_WALLET",
    # HIPAA Safe Harbor extra identifiers (provider/device/vehicle/web)
    "US_DEA",
    "US_NPI",
    "MAC_ADDRESS",
    "IMEI",
    "VIN",
    "FDA_UDI",
    "UUID",
    "BEARER_TOKEN",
    "USERNAME_HANDLE",
    # GDPR Art.9 — genetic data
    "GENETIC_VARIANT",
]

# ---------------------------------------------------------------------------
# Presidio engine singleton (lazy, thread-safe)
# ---------------------------------------------------------------------------

# Languages for which custom recognizers are registered.
# Extended to ["en", "fr"] at engine init when fr_core_news_lg is installed.
_SUPPORTED_LANGS: list[str] = ["en"]

_ENGINE_LOCK = threading.Lock()
_ANALYZER = None


def _build_custom_recognizers():
    """Build PatternRecognizer instances for PII types not covered by Presidio builtins."""
    from presidio_analyzer import Pattern, PatternRecognizer

    recognizers = []

    def _add(entity, name, patterns, context=None):
        # Register one recognizer instance per supported language so that
        # Presidio's per-language filter doesn't discard regex matches when
        # language="fr" (or any future lang) is passed by the caller.
        for lang in _SUPPORTED_LANGS:
            recognizers.append(
                PatternRecognizer(
                    supported_entity=entity,
                    name=f"{name}_{lang}",
                    patterns=[
                        Pattern(name=n, regex=r, score=s) for n, r, s in patterns
                    ],
                    supported_language=lang,
                    context=context or [],
                )
            )

    # --- Addresses ---
    _add(
        "STREET_ADDRESS",
        "us_address_recognizer",
        [
            (
                "us_address",
                (
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
                ),
                0.85,
            ),
        ],
    )

    _add(
        "STREET_ADDRESS",
        "eu_address_recognizer",
        [
            (
                "de_address",
                (
                    r"\b(?:[A-ZÄÖÜ][a-zäöüß]+(?:straße|strasse|str\.?|gasse|weg|platz|allee|ring|damm|ufer))"
                    r"\s+\d{1,5}(?:\s?[a-zA-Z])?"
                ),
                0.85,
            ),
            (
                "fr_address",
                (
                    r"\b(?:(?:Rue|Avenue|Boulevard|Bd\.?|Chemin|Place|Allée|Impasse|Passage|Quai)"
                    r"(?:\s+(?:de|du|des|la|le|l')?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
                    r"\s+\d{1,5}"
                ),
                0.85,
            ),
            # French standard: number FIRST — "10 Rue de Paris", "25 bis Avenue Victor Hugo"
            (
                "fr_address_num_first",
                (
                    r"\b\d{1,5}\s*(?:bis|ter)?\s+"
                    r"(?:Rue|Avenue|Av\.?|Boulevard|Bd\.?|Chemin|Place|Allée|Impasse|Passage|Quai|Cours|Square)"
                    r"(?:\s+(?:de|du|des|la|le|l')?\s*[A-ZÀ-Ü][A-Za-zà-ü\-]+){1,4}\b"
                ),
                0.85,
            ),
            (
                "it_address",
                (
                    r"\b(?:(?:Via|Viale|Piazza|Corso|Largo|Vicolo)"
                    r"(?:\s+(?:dei?|del|della|delle|degli)?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
                    r"\s*,?\s*\d{1,5}"
                ),
                0.85,
            ),
            (
                "es_address",
                (
                    r"\b(?:(?:Calle|Avenida|Avda\.?|Paseo|Plaza|Carrera|Camino)"
                    r"(?:\s+(?:de|del|la|las|los)?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
                    r"\s*,?\s*\d{1,5}"
                ),
                0.85,
            ),
            (
                "nl_address",
                (
                    r"\b(?:[A-Z][a-z]+(?:straat|laan|weg|gracht|plein|singel|kade|dijk))"
                    r"\s+\d{1,5}(?:\s?[a-zA-Z])?"
                ),
                0.85,
            ),
            (
                "po_box",
                (
                    r"\b(?:P\.?O\.?\s*Box|Postfach|Boîte\s+Postale|BP|Casella\s+Postale|CP|"
                    r"Apartado(?:\s+de\s+Correos)?|Postbus)\s*[:#]?\s*\d{1,10}\b"
                ),
                0.85,
            ),
        ],
    )

    # --- Phone / Fax ---
    _add(
        "INTL_PHONE",
        "intl_phone_recognizer",
        [
            # Negative lookbehind on \d AND on alphabetic IBAN / BIC chars to avoid
            # matching mid-IBAN substrings like "0044 0532 0130 00" inside a DE89 IBAN.
            (
                "intl_phone",
                (
                    r"(?<![\dA-Z])(?:\+|00)\d{1,3}"
                    r"(?:[.\-\s]?\(?\d{1,5}\)?)+"
                    r"(?:[.\-\s]?\d{2,5}){1,4}(?!\d)"
                ),
                0.6,
            ),
        ],
    )

    _add(
        "FAX_NUMBER",
        "fax_recognizer",
        [
            (
                "fax",
                (
                    r"\b(?:fax|telefax|facsimile)\s*[:#\-]?\s*"
                    r"(?:\+|00)?[\d\s.\-()]{7,20}"
                ),
                0.85,
            ),
        ],
        context=["fax", "telefax", "facsimile"],
    )

    # --- EU National Identifiers ---
    _add(
        "UK_NINO",
        "uk_nino_recognizer",
        [
            (
                "nino",
                (
                    r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)"
                    r"[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]"
                    r"\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"
                ),
                0.85,
            ),
        ],
        context=["national insurance", "NI number", "NINO"],
    )

    _add(
        "FR_NIR",
        "fr_nir_recognizer",
        [
            ("nir", r"\b[12]\s?\d{2}\s?\d{2}\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}\b", 0.7),
        ],
        context=["NIR", "sécurité sociale", "sécu"],
    )

    _add(
        "DE_SVNR",
        "de_svnr_recognizer",
        [
            ("svnr", r"\b\d{2}\s?\d{6}\s?[A-Z]\s?\d{3}\b", 0.7),
        ],
        context=["Sozialversicherung", "SVNR", "Versicherungsnummer"],
    )

    _add(
        "NL_BSN",
        "nl_bsn_recognizer",
        [
            (
                "bsn_labeled",
                (r"\b(?:BSN|burgerservicenummer)\s*[:#\-]?\s*\d{9}\b"),
                0.85,
            ),
        ],
        context=["BSN", "burgerservicenummer"],
    )

    _add(
        "IT_CF",
        "it_cf_recognizer",
        [
            (
                "codice_fiscale",
                r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b",
                0.9,
            ),
        ],
        context=["codice fiscale", "CF"],
    )

    _add(
        "ES_DNI",
        "es_dni_recognizer",
        [
            ("dni", r"\b(?:\d{8}[A-Z]|[XYZ]\d{7}[A-Z])\b", 0.85),
        ],
        context=["DNI", "NIE", "documento"],
    )

    _add(
        "CH_AHV",
        "ch_ahv_recognizer",
        [
            ("ahv", r"\b756\.\d{4}\.\d{4}\.\d{2}\b", 0.95),
        ],
    )

    _add(
        "BE_NN",
        "be_nn_recognizer",
        [
            ("nn_be", r"\b\d{2}\.\d{2}\.\d{2}-\d{3}\.\d{2}\b", 0.85),
        ],
        context=["rijksregister", "national number"],
    )

    # --- Healthcare IDs ---
    _add(
        "MRN",
        "mrn_recognizer",
        [
            (
                "mrn",
                (
                    r"\b(?:"
                    r"MRN|medical\s+record(?:\s+number)?|"
                    # "patient id" / "patient #" but NOT "patient identifies" — require non-alpha after keyword
                    r"patient\s*(?:id(?!entif)|number|no\.?|#|identifier(?:\s+number)?)|"
                    r"hospital\s*(?:id(?!entif)|number|no\.?|#)|"
                    r"case\s*(?:id(?!entif)|number|no\.?|#)|"
                    r"encounter\s*(?:id(?!entif)|number|no\.?)|"
                    r"visit\s*(?:id(?!entif)|number|no\.?)|"
                    r"chart\s*(?:id(?!entif)|number|no\.?)|"
                    r"Fallnummer|Fall-?Nr\.?|Patientennummer|Pat\.?\s*Nr\.?|Patientennr\.?|"
                    r"numéro\s+de\s+dossier|n°?\s*dossier|IPP|NDA|"
                    r"número\s+de\s+historia|NHC|"
                    r"patiëntnummer|dossiernummer"
                    r")[\s:#\-]+(?=[A-Z0-9\-]{2,30}\b)(?=[A-Z0-9\-]*\d)[A-Z0-9\-]{2,30}\b"
                ),
                0.85,
            ),
        ],
        context=["MRN", "medical record", "patient"],
    )

    _add(
        "UK_NHS",
        "uk_nhs_recognizer",
        [
            (
                "nhs",
                (
                    r"\b(?:NHS(?:\s+number)?[:\s\-]*)?(?<!\d)\d{3}\s?\d{3}\s?\d{4}(?!\d)\b"
                ),
                0.5,
            ),
        ],
        context=["NHS", "NHS number"],
    )

    _add(
        "DE_KVNR",
        "de_kvnr_recognizer",
        [
            ("kvnr", r"\b[A-Z]\d{9}\b", 0.5),
        ],
        context=["Krankenversichertennummer", "KVNR", "Versichertennummer"],
    )

    _add(
        "EU_EHIC",
        "eu_ehic_recognizer",
        [
            (
                "ehic",
                (
                    r"\b(?:EHIC|European\s+Health\s+Insurance|CEAM|TEAM|Carte\s+Européenne|"
                    r"Gesundheitskarte|tessera\s+sanitaria)"
                    r"(?:\s+(?:card|number|no\.?|nr\.?|#))?"
                    r"\s*[:#\-]?\s*[A-Z0-9\s\-]{8,80}\b"
                ),
                0.85,
            ),
        ],
        context=["EHIC", "health insurance card"],
    )

    # --- Postcodes ---
    _add(
        "UK_POSTCODE",
        "uk_postcode_recognizer",
        [
            ("uk_postcode", r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", 0.6),
        ],
        context=["postcode", "post code", "zip"],
    )

    _add(
        "EU_POSTCODE",
        "eu_postcode_recognizer",
        [
            ("nl_postcode", r"\b\d{4}\s?[A-Z]{2}\b", 0.6),
            ("pl_postcode", r"\b\d{2}-\d{3}\b", 0.5),
            # SE postcode "123 45" — too generic alone, requires SE/Sweden context bonus
            ("se_postcode", r"\b\d{3}\s\d{2}\b", 0.15),
            # Bare 5-digit FR/DE/ES/IT postcode followed by capitalised city name —
            # high-recall pattern, low confidence so context bonus / city co-location is required.
            # Inline (?-i:) override forces case-sensitive capital match for city since
            # Presidio applies re.IGNORECASE globally.
            ("eu5_with_city", r"\b\d{5}\s+(?-i:[A-ZÀ-Ü])[A-Za-zà-ü\-]{2,}\b", 0.55),
            # Bare 4-digit BE/CH/AT/LU postcode followed by city name
            ("eu4_with_city", r"\b\d{4}\s+(?-i:[A-ZÀ-Ü])[A-Za-zà-ü\-]{2,}\b", 0.45),
        ],
        context=[
            "postcode",
            "PLZ",
            "code postal",
            "código postal",
            "CAP",
            "Postleitzahl",
            "postnummer",
            "Sweden",
        ],
    )

    # --- Strong SSN (override Presidio built-in low-confidence pattern) ---
    _add(
        "US_SSN",
        "us_ssn_strong_recognizer",
        [
            # Standard SSN format — high confidence even without context label
            (
                "ssn_dashed",
                r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b",
                0.85,
            ),
        ],
        context=["SSN", "social security", "social-security"],
    )

    # --- SWIFT / BIC code ---
    _add(
        "SWIFT_BIC",
        "swift_bic_recognizer",
        [
            # Only fire on labeled BIC — bare 8-letter word would match any English word.
            # (?-i:...) forces case-sensitive uppercase to defeat Presidio's IGNORECASE.
            (
                "bic_labeled",
                (
                    r"\b(?:BIC|SWIFT)(?:[\s:#\-]+)"
                    r"(?-i:[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b"
                ),
                0.9,
            ),
        ],
        context=["BIC", "SWIFT", "bank code"],
    )

    # --- EU VAT / Tax identification number ---
    _add(
        "EU_VAT",
        "eu_vat_recognizer",
        [
            # Unlabeled pattern: country code MUST be uppercase, value MUST contain a digit
            (
                "vat",
                (
                    r"\b(?-i:(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|GB|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI))"
                    r"[\s\-]?(?=[A-Z0-9\s\-]*\d)(?-i:[A-Z0-9])(?-i:[A-Z0-9\s\-]){6,14}(?-i:[A-Z0-9])\b"
                ),
                0.4,
            ),
            (
                "vat_labeled",
                (
                    r"\b(?:VAT|TVA|UID|USt[\-]?IdNr\.?|Steuernummer|IVA|BTW|NIF|partita\s+IVA)"
                    r"[\s:#\-]+"
                    r"(?:(?-i:(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|GB|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI))[\s\-]?)?"
                    r"(?=[A-Z0-9\s\-]*\d)(?-i:[A-Z0-9])(?-i:[A-Z0-9\s\-]){6,14}(?-i:[A-Z0-9])\b"
                ),
                0.9,
            ),
        ],
        context=["VAT", "TVA", "UID", "Steuer", "IVA", "BTW", "NIF"],
    )

    # --- Labeled Markers ---
    _add(
        "DOB_MARKER",
        "dob_marker_recognizer",
        [
            (
                "dob",
                (
                    r"\b(?:DOB|D\.O\.B\.?|date\s+of\s+birth|born(?:\s+on)?|birth\s*date|"
                    r"Geburtsdatum|date\s+de\s+naissance|fecha\s+de\s+nacimiento|"
                    r"data\s+di\s+nascita|geboortedatum)"
                    r"[:\s\-]+\d{1,4}[\.\-/]\d{1,2}[\.\-/]\d{2,4}\b"
                ),
                0.95,
            ),
        ],
        context=["DOB", "date of birth", "born"],
    )

    _add(
        "AGE",
        "age_marker_recognizer",
        [
            # "age: 45" / "aged 45" — keyword must be followed immediately by separator+digit
            (
                "age_labeled",
                (r"\b(?:age|aged|Alter|âge|edad|età)\s*[:\-]\s*\d{1,3}\b"),
                0.85,
            ),
            # "60 year-old" / "60-year-old" / "60 yr old"
            ("age_years_old", (r"\b\d{1,3}\s*-?\s*(?:year|yr)s?\s*-?\s*old\b"), 0.85),
            (
                "age_eu_lang",
                (r"\b\d{1,3}\s*(?:Jahre?\s+alt|ans\b|años\b|anni\b)\b"),
                0.85,
            ),
            # Infant/pediatric: "10 month-old", "3 weeks old", "5 day-old"
            ("age_months_old", r"\b\d{1,2}\s*-?\s*months?\s*-?\s*old\b", 0.85),
            ("age_weeks_old", r"\b\d{1,3}\s*-?\s*weeks?\s*-?\s*old\b", 0.85),
            ("age_days_old", r"\b\d{1,3}\s*-?\s*days?\s*-?\s*old\b", 0.85),
            # Age ranges: "0-9 year-old", "6-12 months"
            (
                "age_range",
                (r"\b\d{1,3}\s*-\s*\d{1,3}\s*(?:year|yr|month|week|day)s?\b"),
                0.75,
            ),
            # Standalone clinical age terms
            ("age_newborn", r"\b(?:newborn|neonate|neonatal)\b", 0.75),
            ("age_infant", r"\b(?:infant|toddler)\b", 0.60),
        ],
        context=["age", "years old", "month-old", "newborn", "infant"],
    )

    _add(
        "NATIONAL_ID_LABEL",
        "national_id_label_recognizer",
        [
            (
                "national_id",
                (
                    # "id" must be followed by a separator or digit, never a letter (blocks "identifies", "ideal", etc.)
                    r"\b(?:national\s*id(?!entif|eal|ea|le|al)|nid\b|passport(?:\s*number)?|"
                    r"id(?!entif|eal|ea|le|al)\s*(?:number|no\.?|#|card)|"
                    r"identity\s*card|carte\s+d'identité|personalausweis|"
                    r"documento\s+de\s+identidad|carta\s+d'identità)\s*"
                    r"[:#\-\s]+[A-Z0-9\-]{4,50}\b"
                ),
                0.85,
            ),
        ],
        context=["national ID", "passport", "identity"],
    )

    _add(
        "ACCOUNT_LABEL",
        "account_label_recognizer",
        [
            (
                "account",
                (
                    r"\b(?:account|acct|insurance|member|policy|Versicherung|"
                    r"assurance|verzekering|seguro|assicurazione)\s*"
                    r"(?:id|number|no\.?|#|nummer|numéro|número)?\s*[:#-]?\s*[A-Z0-9\-]{4,50}\b"
                ),
                0.7,
            ),
        ],
        context=["account", "insurance", "policy"],
    )

    _add(
        "LICENSE_PLATE",
        "license_plate_recognizer",
        [
            (
                "plate",
                (
                    # "registration" alone is too broad — require "plate" or vehicle context words
                    r"\b(?:license\s*plate|number\s*plate|Kennzeichen|immatriculation|"
                    r"matrícula|targa|kenteken)\s*[:#\-]?\s*[A-Z0-9\-\s]{3,12}\b"
                ),
                0.85,
            ),
        ],
        context=["license plate", "vehicle registration", "vehicle"],
    )

    # --- GDPR Art.9 Special Categories ---
    _add(
        "NATIONALITY_LABEL",
        "nationality_recognizer",
        [
            (
                "nationality",
                (
                    r"\b(?:nationality|citizenship|Staatsangehörigkeit|nationalité|"
                    r"nacionalidad|nazionalità|nationaliteit)\s*[:\-]\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,40}\b"
                ),
                0.85,
            ),
        ],
    )

    _add(
        "RELIGION_LABEL",
        "religion_recognizer",
        [
            (
                "religion",
                (
                    r"\b(?:religion|faith|confession|Konfession|Glaube|"
                    r"religión|religione|religie|croyance)\s*[:\-]\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,40}\b"
                ),
                0.85,
            ),
        ],
    )

    _add(
        "POLITICAL_LABEL",
        "political_recognizer",
        [
            (
                "political",
                (
                    r"\b(?:political(?:\s+opinion|\s+affiliation)?|party|"
                    r"politische\s+(?:Meinung|Überzeugung)|parti\s+politique|"
                    r"afiliación\s+política)\s*[:\-]\s*"
                    r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,80}\b"
                ),
                0.85,
            ),
        ],
    )

    _add(
        "ETHNICITY_LABEL",
        "ethnicity_recognizer",
        [
            (
                "ethnicity",
                (
                    r"\b(?:ethnic(?:ity)?|ethnische\s+Herkunft|race|racial\s+origin|"
                    r"origine\s+(?:ethnique|razziale)|origen\s+étnico|etniciteit)"
                    r"\s*[:\-]\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-\s]{1,40}\b"
                ),
                0.85,
            ),
        ],
    )

    # --- EU Date Formats ---
    _add(
        "EU_DATE",
        "eu_date_recognizer",
        [
            ("date_eu", r"\b\d{1,2}[.\-]\d{1,2}[.\-]\d{2,4}\b", 0.4),
        ],
    )

    _add(
        "EU_DATE_WRITTEN",
        "eu_date_written_recognizer",
        [
            (
                "date_written_eu",
                (
                    r"\b\d{1,2}\.?\s*(?:"
                    r"Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember|"
                    r"janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre|"
                    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|"
                    r"gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre|"
                    r"januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|"
                    r"janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
                    r")\s+\d{4}\b"
                ),
                0.85,
            ),
        ],
    )

    # --- Gender (bare inline, no label prefix) ---
    # "male", "female", "non-binary" etc. as they appear in clinical note
    # demographics — consistent with structured Patient.gender redaction.
    #
    # The bare "male"/"female" pattern uses a base score (0.35) BELOW the
    # default detection threshold (0.4) on purpose: it only fires when
    # Presidio applies the +0.4 context-word boost from the surrounding
    # narrative.  This keeps SNOMED/LOINC display labels such as
    # "Female sterilization procedure" or "Male reproductive system" from
    # being mis-tagged when no demographic context is present, while still
    # catching "the patient is a 45 year old male" in clinical notes.
    # Extended terms (non-binary, transgender, intersex, …) are unambiguous
    # and use a high base score regardless of context.
    _add(
        "GENDER",
        "gender_recognizer",
        [
            ("gender_binary", r"\b(?:male|female)\b", 0.35),
            (
                "gender_extended",
                (
                    r"\b(?:non[\s\-]?binary|nonbinary|"
                    r"trans(?:gender)?(?:\s+(?:male|female|man|woman))?|"
                    r"trans(?:man|woman|masc|femme)|"
                    r"gender[\s\-](?:non[\s\-]?conforming|fluid|queer)|"
                    r"intersex|genderqueer|agender|bigender)\b"
                ),
                0.85,
            ),
        ],
        context=[
            "patient",
            "year",
            "old",
            "sex",
            "gender",
            "history",
            "race",
            "ethnicity",
            "complaint",
            "demographics",
            "presents",
            "presented",
            "admitted",
        ],
    )

    # --- Race / Ethnicity (bare inline, no label prefix) ---
    # "nonhispanic white female", "African American", "Hispanic" etc. as they
    # appear in clinical notes — distinct from ETHNICITY_LABEL which requires
    # the "ethnicity:" label prefix.
    _add(
        "RACE_ETHNICITY",
        "race_ethnicity_recognizer",
        [
            (
                "race_eth",
                (
                    r"\b(?:non-?\s*hispanic\s+)?"
                    r"(?:white|caucasian|"
                    r"black|african[\s\-]american|afro[\s\-]caribbean|afro[\s\-]american|"
                    r"hispanic|latino|latina|latinx|latin[\s\-]american|"
                    r"asian|east[\s\-]asian|south[\s\-]asian|southeast[\s\-]asian|"
                    r"pacific[\s\-]islander|native[\s\-]hawaiian|"
                    r"native[\s\-]american|american[\s\-]indian|alaskan[\s\-]native|alaska[\s\-]native|"
                    r"indigenous|aboriginal|"
                    r"multiracial|bi[\s\-]racial|mixed[\s\-]race|mixed[\s\-]heritage|"
                    r"middle[\s\-]eastern|north[\s\-]african|"
                    r"non[\s\-]hispanic)"
                    r"\b"
                ),
                0.75,
            ),
        ],
        context=["race", "ethnicity", "ethnic", "origin", "descent"],
    )

    # --- Insurance / Coverage Status ---
    # "NO INSURANCE", "Medicare", "self-pay", insurer names
    _add(
        "INSURANCE_STATUS",
        "insurance_status_recognizer",
        [
            (
                "uninsured",
                (
                    r"\b(?:no\s+insurance|NO\s+INSURANCE|uninsured|under[\s\-]insured|"
                    r"no\s+(?:health\s+)?coverage|without\s+(?:health\s+)?insurance|"
                    r"self[\s\-]pay|private\s+pay|cash[\s\-]pay(?:er)?|out[\s\-]of[\s\-]pocket)\b"
                ),
                0.85,
            ),
            (
                "public_program",
                (
                    r"\b(?:Medicare|Medicaid|CHIP|S[\s\-]CHIP|"
                    r"Children'?s?\s+Health\s+Insurance\s+Program|"
                    r"Medi[\s\-]Cal|TennCare|BadgerCare|CoverKids|"
                    r"VA\s+(?:health\s+)?(?:insurance|benefits|coverage)|TRICARE|CHAMPVA)\b"
                ),
                0.85,
            ),
            (
                "private_insurer",
                (
                    r"\b(?:Blue\s+Cross|Blue\s+Shield|BCBS|Aetna|Cigna|Humana|"
                    r"UnitedHealth(?:care)?|United\s+Health(?:care)?|Anthem|Centene|Molina|"
                    r"Kaiser(?:\s+Permanente)?|WellCare|AmeriHealth|Magellan|"
                    r"Oxford\s+Health|HealthNet|Health\s+Net)\b"
                ),
                0.75,
            ),
        ],
        context=["insurance", "coverage", "payer", "plan", "insurer"],
    )

    # --- Healthcare facility names (hospitals, clinics, medical centres) ---
    # spaCy NER mis-classifies many EU multi-lingual facility names; an explicit
    # regex covers the most common naming conventions across EN/FR/DE/IT/ES/NL.
    # Common label tokens (SSN, IBAN, BIC, VAT, DOB, MRN, NIR, etc.) are excluded
    # to prevent false positives where spaCy NER over-tags acronyms as ORG.
    _ORG_DENY = (
        r"SSN|IBAN|BIC|SWIFT|VAT|TVA|UID|DOB|MRN|NHS|NIR|BSN|DNI|CF|NIE|EHIC|"
        r"PLZ|ZIP|TEL|FAX|EMAIL|URL|HTTP|HTTPS|API|UUID|ICD|CPT|LOINC|SNOMED|"
        r"NDC|RXNORM|HL7|FHIR|XML|JSON|PDF|CSV|GDPR|HIPAA|EHR|EMR|PHI|PII"
    )
    # Facility-type keyword catalogue (trailing keyword). The original set only
    # covered Hospital/Clinic/Medical Center/Health Center/Health System, which
    # missed the bulk of US ambulatory-care naming that Synthea and real EHRs
    # emit in ALL CAPS (e.g. "ST FRANCIS URGENT CARE", "BOSTON MEDICAL GROUP",
    # "NEW ENGLAND HOME HEALTH"). spaCy statistical NER only partially tags such
    # ALL-CAPS spans, leaking the distinguishing name tokens; this deterministic
    # regex captures the full span. Multi-word phrases are used deliberately so
    # bare English words ("care", "health") do not over-match narrative text.
    _facility_kw = (
        r"Hospital|Clinic|Medical\s+Cent(?:er|re)|Health\s+Cent(?:er|re)|Health\s+System|"
        r"Urgent\s+Care|Medical\s+Group|Medical\s+Associates|Medical\s+Practice|"
        r"Health\s+Partners|Health\s+Network|Health\s+Services|Health\s+Care|Healthcare|"
        r"Community\s+Health(?:\s+Cent(?:er|re))?|Home\s+Health|Hospice|"
        r"Nursing\s+(?:Home|Cent(?:er|re)|Facility)|Skilled\s+Nursing(?:\s+Facility)?|"
        r"Rehabilitation(?:\s+(?:Cent(?:er|re)|Hospital))?|Rehab\s+Cent(?:er|re)|"
        r"Assisted\s+Living|Surg(?:ery|ical)\s+Cent(?:er|re)|Ambulatory\s+Surgery\s+Cent(?:er|re)|"
        r"Cancer\s+Cent(?:er|re)|Oncology\s+Cent(?:er|re)|Dialysis(?:\s+Cent(?:er|re))?|"
        r"Imaging(?:\s+Cent(?:er|re))?|Radiology|Diagnostic(?:s|\s+Cent(?:er|re))|"
        r"Laborator(?:y|ies)|Family\s+Practice|Family\s+Medicine|Family\s+Health(?:\s+Cent(?:er|re))?|"
        r"Internal\s+Medicine|Primary\s+Care|Pediatric(?:s)?|Physical\s+Therapy|"
        r"Behavioral\s+Health|Mental\s+Health|Wellness\s+Cent(?:er|re)|Women'?s\s+Health|"
        r"Care\s+Cent(?:er|re)|Adult\s+Day\s+(?:Health|Care)|Day\s+(?:Health|Care)\s+Cent(?:er|re)|"
        r"Long[\s\-]Term\s+Care|"
        # EU equivalents (unchanged)
        r"Hôpital|Clinique|Centre\s+Hospitalier|Centre\s+Médical|Polyclinique|"
        r"Krankenhaus|Klinik|Klinikum|Universitätsklinikum|"
        r"Ospedale|Policlinico|Clinica|Clínica|Centro\s+Médico|Ziekenhuis|Kliniek"
    )
    # Legal-entity suffixes — a strong organisation signal on their own. Absorbed
    # into the facility span (so "ACME MEDICAL ASSOCIATES, LLC" masks whole) and
    # also matched standalone for named orgs with no facility keyword
    # (e.g. "ACME HEALTH SOLUTIONS LLC").
    _org_corp = (
        r"Inc|Incorporated|LLC|P\.?L\.?L\.?C|L\.?L\.?P|P\.?C|P\.?A|"
        r"Corp|Corporation|Ltd|GmbH|AG"
    )
    _add(
        "ORGANIZATION",
        "healthcare_org_recognizer",
        [
            # "<Name> Hospital/Clinic/Urgent Care/..." — trailing keyword, optional
            # trailing legal-entity suffix absorbed into the span.
            (
                "facility_suffix",
                (
                    r"\b(?!(?:"
                    + _ORG_DENY
                    + r")\b)(?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+(?:\s+(?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+){0,4}"
                    r"\s+(?:" + _facility_kw + r")"
                    r"(?:[\s,]+(?:" + _org_corp + r")\.?)?\b"
                ),
                0.8,
            ),
            # "Hospital/Clinic <Name>" — leading keyword (covers "Clinique Saint-Louis", "Hôpital Necker")
            (
                "facility_prefix",
                (
                    r"\b(?:Hospital|Clinic|Medical\s+Center|Hôpital|Clinique|Polyclinique|"
                    r"Centre\s+Hospitalier|CHU|CHR|Krankenhaus|Klinik|Klinikum|"
                    r"Ospedale|Policlinico|Clinica|Clínica|Ziekenhuis|Kliniek)"
                    r"\s+(?:de\s+|du\s+|des\s+|der\s+|di\s+|del\s+|de\s+la\s+|van\s+)?"
                    r"(?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+(?:[\s\-](?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+){0,3}\b"
                ),
                0.85,
            ),
            # Insurance / payer suffix — "AXA Health France", "Blue Cross", "Aetna Inc"
            (
                "payer_suffix",
                (
                    r"\b(?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+(?:\s+(?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+){0,3}"
                    r"\s+(?:Insurance|Health|Healthcare|Assurance|Versicherung|Mutuelle|Mutua|"
                    r"Krankenkasse|Krankenversicherung|Assicurazione|Verzekering)\b"
                ),
                0.7,
            ),
            # Named organisation with a legal-entity suffix but no facility keyword —
            # "<Name...> LLC/Inc/PC/Corp". Legal suffixes almost never occur in
            # clinical narrative, so this stays high-precision.
            (
                "corporate_suffix",
                (
                    r"\b(?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+(?:[\s,]+(?-i:[A-ZÀ-Ü])[\wÀ-ÿ'\-]+){0,4}"
                    r"\s*,?\s+(?:" + _org_corp + r")\.?\b"
                ),
                0.6,
            ),
        ],
        context=[
            "hospital",
            "clinic",
            "clinique",
            "krankenhaus",
            "ospedale",
            "insurance",
            "payer",
        ],
    )

    # Decimal pairs, labeled lat/lon, and DMS format — all appear in FHIR
    # address extensions and Synthea-generated Location resources.
    (
        _add(
            "GEO_COORDINATES",
            "geo_coordinates_recognizer",
            [
                # Decimal pair: "42.123456, -71.234567"
                (
                    "decimal_coords",
                    (
                        r"[-+]?\b\d{1,3}\.\d{4,10}"
                        r"\s*[,;]\s*"
                        r"[-+]?\d{1,3}\.\d{4,10}\b"
                    ),
                    0.85,
                ),
                # Labeled: "lat: 42.123, lon: -71.456" / "latitude: ... longitude: ..."
                (
                    "labeled_coords",
                    (
                        r"\b(?:lat(?:itude)?)\s*[:\-=]\s*[-+]?\d{1,3}\.\d{2,10}"
                        r"[\s,;]+"
                        r"(?:lon(?:g(?:itude)?)?)\s*[:\-=]\s*[-+]?\d{1,3}\.\d{2,10}\b"
                    ),
                    0.9,
                ),
                # DMS: "42°23'15"N 71°14'05"W"
                (
                    "dms_coords",
                    (
                        r"\b\d{1,3}°\s*\d{1,2}\'\s*\d{1,2}(?:\.\d+)?[\"″]?\s*[NSns]"
                        r"\s+"
                        r"\d{1,3}°\s*\d{1,2}\'\s*\d{1,2}(?:\.\d+)?[\"″]?\s*[EWew]\b"
                    ),
                    0.9,
                ),
            ],
        ),
    )

    # --- Synthetic Data Artifacts ---
    _add(
        "SYNTHEA_SEED",
        "synthea_seed_recognizer",
        [
            ("synthea_seed", r"\b(?:Person|Population)\s+seed\s*:\s*-?\d{5,}\b", 0.95),
        ],
    )

    # --- Synthea-generated names (CapWord + digits, e.g. "Johnathan123 Doe456") ---
    # Synthea appends numeric suffixes to first/last names; spaCy NER misses these.
    _add(
        "PERSON",
        "synthea_person_recognizer",
        [
            ("synthea_person", r"\b[A-Z][a-z]+\d+(?:\s+[A-Z][a-z]+\d*)+\b", 0.85),
        ],
    )

    # ============================================================
    # HIPAA Safe Harbor §164.514(b)(2)(i) additional identifiers
    # ============================================================

    # (E) Provider DEA number (US Drug Enforcement Administration)
    # Format: 2 letters + 7 digits, where last digit is checksum
    _add(
        "US_DEA",
        "us_dea_recognizer",
        [
            (
                "dea_labeled",
                (
                    r"\b(?:DEA|D\.E\.A\.?)(?:\s+(?:number|no\.?|#))?\s*[:#\-]?\s*"
                    r"(?-i:[A-Z]{2})\d{7}\b"
                ),
                0.9,
            ),
            ("dea_bare", r"\b(?-i:[A-Z]{2})\d{7}\b", 0.4),
        ],
        context=["DEA", "prescriber", "controlled substance"],
    )

    # (E) US National Provider Identifier — 10 digits with Luhn-style check
    _add(
        "US_NPI",
        "us_npi_recognizer",
        [
            (
                "npi_labeled",
                (
                    r"\b(?:NPI|National\s+Provider\s+(?:Identifier|ID))"
                    r"(?:\s+(?:number|no\.?|#))?\s*[:#\-]?\s*\d{10}\b"
                ),
                0.9,
            ),
        ],
        context=["NPI", "provider", "physician", "clinician"],
    )

    # (M) Device identifiers — MAC address (IEEE 802 EUI-48 / EUI-64)
    _add(
        "MAC_ADDRESS",
        "mac_address_recognizer",
        [
            ("mac_colon", r"\b(?-i:[0-9A-F]{2}(?::[0-9A-F]{2}){5})\b", 0.85),
            ("mac_dash", r"\b(?-i:[0-9A-F]{2}(?:-[0-9A-F]{2}){5})\b", 0.85),
        ],
        context=["MAC", "ethernet", "BSSID"],
    )

    # (M) Mobile device IMEI — 15 digits with Luhn check
    _add(
        "IMEI",
        "imei_recognizer",
        [
            ("imei_labeled", r"\b(?:IMEI|MEID)\s*[:#\-]?\s*\d{15}\b", 0.95),
            ("imei_bare", r"(?<!\d)\d{15}(?!\d)", 0.3),
        ],
        context=["IMEI", "MEID", "mobile", "phone"],
    )

    # (I) Vehicle identifier — VIN (17 chars, no I/O/Q)
    _add(
        "VIN",
        "vin_recognizer",
        [
            (
                "vin_labeled",
                (
                    r"\b(?:VIN|chassis\s+(?:number|no\.?))\s*[:#\-]?\s*"
                    r"(?-i:[A-HJ-NPR-Z0-9]{17})\b"
                ),
                0.95,
            ),
            ("vin_bare", r"\b(?-i:[A-HJ-NPR-Z0-9]{17})\b", 0.4),
        ],
        context=["VIN", "vehicle", "chassis"],
    )

    # (M) FDA Unique Device Identifier — barcode/string with parenthesized AIs
    _add(
        "FDA_UDI",
        "fda_udi_recognizer",
        [
            (
                "udi",
                (
                    r"(?:\(01\)\d{14}|\(11\)\d{6}|\(17\)\d{6}|\(10\)[A-Z0-9]{1,20}|\(21\)[A-Z0-9]{1,20})"
                    r"(?:\(01\)\d{14}|\(11\)\d{6}|\(17\)\d{6}|\(10\)[A-Z0-9]{1,20}|\(21\)[A-Z0-9]{1,20})*"
                ),
                0.95,
            ),
            ("udi_labeled", (r"\b(?:UDI|GTIN)\s*[:#\-]?\s*\d{10,20}\b"), 0.85),
        ],
        context=["UDI", "GTIN", "device identifier"],
    )

    # (R) Universally Unique Identifier (UUID v1-v5) — generic but identifying
    _add(
        "UUID",
        "uuid_recognizer",
        [
            (
                "uuid",
                (
                    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
                    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
                ),
                0.85,
            ),
        ],
    )

    # (P) Account numbers — cryptocurrency wallet addresses
    _add(
        "CRYPTO_WALLET",
        "crypto_wallet_recognizer",
        [
            # Bitcoin Legacy (P2PKH/P2SH): starts with 1 or 3, base58, 26-35 chars
            ("btc_legacy", r"\b(?-i:[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b", 0.6),
            # Bitcoin Bech32 (SegWit): bc1...
            ("btc_bech32", r"\b(?-i:bc1)[a-zA-HJ-NP-Z0-9]{25,62}\b", 0.85),
            # Ethereum: 0x + 40 hex
            ("eth", r"\b0x[a-fA-F0-9]{40}\b", 0.85),
        ],
        context=["wallet", "bitcoin", "ethereum", "BTC", "ETH", "crypto"],
    )

    # (R) Authentication tokens — Bearer / JWT / API keys
    _add(
        "BEARER_TOKEN",
        "bearer_token_recognizer",
        [
            # JWT: 3 base64url segments separated by dots
            (
                "jwt",
                r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
                0.95,
            ),
            # Bearer / Basic auth headers
            (
                "bearer_header",
                (r"\b(?:Bearer|Basic|Token)\s+[A-Za-z0-9_\-\.=+/]{16,}\b"),
                0.9,
            ),
            # Generic API-key-shaped labels
            (
                "api_key",
                (
                    r"\b(?:api[_\-]?key|access[_\-]?token|secret[_\-]?key|auth[_\-]?token)"
                    r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.=+/]{16,}[\"']?"
                ),
                0.85,
            ),
        ],
        context=["bearer", "token", "api key", "authorization"],
    )

    # (R) Username / handle — social media @handles
    _add(
        "USERNAME_HANDLE",
        "username_handle_recognizer",
        [
            ("at_handle", r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{2,29}\b", 0.6),
        ],
    )

    # (S) US ABA bank routing number — 9 digits with checksum
    _add(
        "US_ROUTING",
        "us_routing_recognizer",
        [
            (
                "aba_labeled",
                (
                    r"\b(?:ABA|routing|RTN|ABA[\s\-]?routing)"
                    r"(?:\s+(?:number|no\.?|#))?\s*[:#\-]?\s*\d{9}\b"
                ),
                0.9,
            ),
        ],
        context=["ABA", "routing", "RTN", "bank"],
    )

    # GDPR Art.9 — Genetic data (HGVS variant nomenclature)
    _add(
        "GENETIC_VARIANT",
        "genetic_variant_recognizer",
        [
            # HGVS: c.5266dupC, c.123A>G, p.Val600Glu, g.456_457delAT
            (
                "hgvs",
                (
                    r"\b[cgpmnr]\.\d+(?:_\d+)?"
                    r"(?:[ACGTU]>[ACGTU]|del[ACGTU]*|dup[ACGTU]*|ins[ACGTU]+|"
                    r"[A-Z][a-z]{2}\d+[A-Z][a-z]{2}|[A-Z]\d+[A-Z*])\b"
                ),
                0.9,
            ),
            # Gene symbol + variant: "BRCA1 c.5266dupC", "TP53 p.R175H"
            (
                "gene_variant",
                (r"\b(?-i:[A-Z][A-Z0-9]{1,9})\s+[cgpmnr]\.[A-Z0-9_>+\-]{2,30}\b"),
                0.85,
            ),
        ],
        context=["mutation", "variant", "gene", "allele", "BRCA", "TP53", "HGVS"],
    )

    return recognizers


def _get_analyzer():
    """Return the process-level Presidio AnalyzerEngine, initializing once."""
    global _ANALYZER, _SUPPORTED_LANGS
    if _ANALYZER is not None:
        return _ANALYZER
    with _ENGINE_LOCK:
        if _ANALYZER is not None:
            return _ANALYZER
        try:
            import spacy.util
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            models = [{"lang_code": "en", "model_name": "en_core_web_lg"}]
            _fr_available = spacy.util.is_package("fr_core_news_lg")
            if _fr_available:
                models.append({"lang_code": "fr", "model_name": "fr_core_news_lg"})
                if "fr" not in _SUPPORTED_LANGS:
                    _SUPPORTED_LANGS.append("fr")
                log.info("fr_core_news_lg detected — enabling French NER")
            else:
                log.info(
                    "fr_core_news_lg not installed — French NER unavailable; "
                    "rebuild with NLP_LANG_FR=true to enable"
                )

            log.info(
                "Initializing Presidio AnalyzerEngine — languages: %s",
                ", ".join(_SUPPORTED_LANGS),
            )
            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": models,
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
            _ANALYZER = AnalyzerEngine(
                nlp_engine=provider.create_engine(),
                supported_languages=list(_SUPPORTED_LANGS),
            )
            custom = _build_custom_recognizers()
            for rec in custom:
                _ANALYZER.registry.add_recognizer(rec)
            log.info(
                "Presidio ready — %d entity types, %d custom recognizers across %d language(s)",
                len(HEALTHCARE_ENTITIES),
                len(custom),
                len(_SUPPORTED_LANGS),
            )
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize Presidio / spaCy.  "
                "Ensure these packages are installed: "
                "presidio-analyzer, spacy, en_core_web_lg.  "
                f"Original error: {exc}"
            ) from exc
    return _ANALYZER
