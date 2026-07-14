-- ─────────────────────────────────────────────────────────────────────────────
-- gPAS domain seed  SPE FHIR de-identification hierarchy + TESTING domain
--
-- PostgreSQL 16 version.
-- Translated from MySQL original: INSERT IGNORE → ON CONFLICT DO NOTHING.
-- All column names are unquoted lowercase to match Hibernate-generated SQL.
--
-- Domain hierarchy mirrors services/gpas/config/domains.json:
--   spe.direct        → 6 children (direct identifiers)
--   spe.quasi         → 3 children (quasi-identifiers)
--   spe.clinical      → 3 children (clinical data)
--   spe.operational   → 2 children (operational workflow)
--   spe.financial     → 2 children (coverage/claims)
--   spe.technical     → 2 children (technical metadata)
-- ─────────────────────────────────────────────────────────────────────────────

-- Runs against the gpas database (public schema).

-- ── Parent domains (insert first  children reference them via FK) ────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('spe.direct', 'Direct Identifiers',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Direct identifying values that should always be pseudonymized before downstream processing.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=dir_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;MULTI_PSN_DOMAIN=true;'),

('spe.quasi', 'Quasi Identifiers',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Values that are not direct identifiers alone but can enable re-identification in combination.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=qsi_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;MULTI_PSN_DOMAIN=true;'),

('spe.clinical', 'Clinical Content',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Clinical data families where different pseudonymization policies may be needed for text, values and references.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=cln_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;MULTI_PSN_DOMAIN=true;'),

('spe.operational', 'Operational and Administrative Workflow',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Scheduling, communication, practitioner and organization related operational identifiers and text.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=ops_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;MULTI_PSN_DOMAIN=true;'),

('spe.financial', 'Coverage, Claims and Billing',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Insurance, claims and account identifiers where separate tenant or legal controls often apply.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=fin_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;MULTI_PSN_DOMAIN=true;'),

('spe.technical', 'Technical Metadata and Linkage',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Transport and linkage metadata that should be isolated from clinical and administrative pseudonym domains.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=tec_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;MULTI_PSN_DOMAIN=true;')
ON CONFLICT (name) DO NOTHING;

-- ── spe.direct children ───────────────────────────────────────────────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('spe.direct.resource-id', 'Logical Resource IDs',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'FHIR Resource.id values across Bundle members and standalone resources.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=rid_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.direct.patient-admin', 'Patient Administrative IDs',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'MRN, SSN, national IDs, passport numbers, local patient identifiers.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=pat_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.direct.person-name', 'Person Names',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Human names for Patient, Practitioner, RelatedPerson, Person, contact names and free-text extracted names.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=nam_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.direct.contact', 'Contact Channels',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Phone numbers, emails, telecom values, endpoint addresses, URLs where pseudonymization rather than hard redaction is required.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=cnt_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.direct.address', 'Addresses and Locations',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Street lines, city, district, postal code, free-form address text, location names and aliases.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=adr_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.direct.document-media', 'Documents, Images and Media',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Attachment titles, document labels, base64 payload references, device serials, UDI values, photos.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=doc_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;')
ON CONFLICT (name) DO NOTHING;

-- ── spe.quasi children ────────────────────────────────────────────────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('spe.quasi.demographic', 'Demographics',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Birth year/date, death dates, gender, marital status, communication language, birthplace, ethnicity, race.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=dem_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.quasi.clinical-time', 'Clinical Timeline',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'onset, abatement, effectiveDateTime, issued, recordedDate, occurrence and other event timing fields.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=dtm_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.quasi.special-category', 'Special Category Attributes',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Nationality, religion, political affiliation/opinion and other Art. 9 style sensitive attributes.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=spc_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;')
ON CONFLICT (name) DO NOTHING;

-- ── spe.clinical children ─────────────────────────────────────────────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('spe.clinical.observation', 'Observations and Measurements',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Observation value sets, reference ranges, component values and related note text.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=obs_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.clinical.condition-procedure', 'Conditions and Procedures',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Condition onset/abatement, Procedure.performed, ClinicalImpression, FamilyMemberHistory and related narrative fields.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=cdp_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.clinical.report-text', 'Reports and Narrative Text',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Narrative.div, notes, DiagnosticReport conclusions, care instructions, comments and other free-text clinical fields.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=txt_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;')
ON CONFLICT (name) DO NOTHING;

-- ── spe.operational children ──────────────────────────────────────────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('spe.operational.org-practitioner', 'Organizations and Practitioners',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Organization, OrganizationAffiliation, Practitioner, PractitionerRole, RelatedPerson, Person and Endpoint related identifiers.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=org_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.operational.scheduling', 'Scheduling and Requests',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Appointment, Schedule, Slot, Task, ServiceRequest, RequestGroup and Communication request payloads.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=sch_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;')
ON CONFLICT (name) DO NOTHING;

-- ── spe.financial children ────────────────────────────────────────────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('spe.financial.coverage', 'Coverage and Eligibility',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Coverage.subscriberId, dependent, class values, network, eligibility request and response identifiers.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=cov_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.financial.claims', 'Claims, EOB and Payments',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Claim, ClaimResponse, ExplanationOfBenefit, Invoice, PaymentReconciliation, Account and ChargeItem identifiers and notes.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=clm_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;')
ON CONFLICT (name) DO NOTHING;

-- ── spe.technical children ────────────────────────────────────────────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('spe.technical.references', 'FHIR References and URLs',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Bundle fullUrl, request.url, response.location, Reference.reference and endpoint/location URLs.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=ref_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'),

('spe.technical.extensions', 'Extensions and Custom Payloads',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Custom extensions, coded displays, expression text and implementation-specific metadata.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=ext_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;')
ON CONFLICT (name) DO NOTHING;

-- ── TESTING domain  default GPAS_DOMAIN for out-of-the-box use ──────────────

INSERT INTO domain (name, label, alphabet, comment, generatorclass, properties) VALUES
('TESTING', 'TESTING',
 'org.emau.icmvc.ganimed.ttp.psn.alphabets.Numbers',
 'Default pseudonymisation domain. Change GPAS_DOMAIN in .env to use an spe.* domain instead.',
 'org.emau.icmvc.ganimed.ttp.psn.generator.Verhoeff',
 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=rid_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;')
ON CONFLICT (name) DO NOTHING;

-- Ensure PSN_PREFIX=rid_ even if the row already existed (ON CONFLICT DO NOTHING skips existing rows)
UPDATE domain
SET properties = 'FORCE_CACHE=DEFAULT;INCLUDE_PREFIX_IN_CHECK_DIGIT_CALCULATION=false;INCLUDE_SUFFIX_IN_CHECK_DIGIT_CALCULATION=false;MAX_DETECTED_ERRORS=2;PSN_LENGTH=10;PSN_PREFIX=rid_;PSN_SUFFIX=;PSNS_DELETABLE=true;USE_LAST_CHAR_AS_DELIMITER_AFTER_X_CHARS=0;'
WHERE name = 'TESTING';

-- Purge old TESTING pseudonyms that were generated without a prefix (purely numeric).
-- They cannot be uploaded to HAPI FHIR (HAPI-0960) and must be regenerated.
-- New pseudonyms will be rid_XXXXXXXXXX, sanitised to rid-XXXXXXXXXX on upload.
DELETE FROM psn WHERE domain = 'TESTING';

-- ── Parent-child relationships ────────────────────────────────────────────────

INSERT INTO domain_parents (domain, parentdomain) VALUES
-- spe.direct children
('spe.direct.resource-id',         'spe.direct'),
('spe.direct.patient-admin',       'spe.direct'),
('spe.direct.person-name',         'spe.direct'),
('spe.direct.contact',             'spe.direct'),
('spe.direct.address',             'spe.direct'),
('spe.direct.document-media',      'spe.direct'),
-- spe.quasi children
('spe.quasi.demographic',          'spe.quasi'),
('spe.quasi.clinical-time',        'spe.quasi'),
('spe.quasi.special-category',     'spe.quasi'),
-- spe.clinical children
('spe.clinical.observation',       'spe.clinical'),
('spe.clinical.condition-procedure','spe.clinical'),
('spe.clinical.report-text',       'spe.clinical'),
-- spe.operational children
('spe.operational.org-practitioner','spe.operational'),
('spe.operational.scheduling',     'spe.operational'),
-- spe.financial children
('spe.financial.coverage',         'spe.financial'),
('spe.financial.claims',           'spe.financial'),
-- spe.technical children
('spe.technical.references',       'spe.technical'),
('spe.technical.extensions',       'spe.technical')
ON CONFLICT (domain, parentdomain) DO NOTHING;
