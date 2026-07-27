---
title: "Glossary"
sidebar_position: 7
description: "Key terms used across the toolkit and this documentation."
---

# Glossary

**Action**, the transformation applied by a rule (e.g. `redact`, `generalize`,
`gpas_pseudonymize`). See [Actions](./actions.md).

**Config profile**, a named, stored set of rules. Selected per request with
`?config_profile=` or as a deployment default. See [Rules](./rules.md).

**`column:` dialect**, the match selector for tabular/SQL data, e.g.
`column:dob` or `table:patients/column:mrn`.

**De-identification**, removing or transforming identifiers so a record can no
longer be attributed to an individual without additional information.

**FHIRPath**, the expression language used to select nodes in FHIR (and CDA/
DICOM resource views), e.g. `Patient.telecom.where(system='phone')`.

**gPAS**, *Generic Pseudonym Administration Service*; the trusted-third-party
(TTP) that issues and resolves **reversible** pseudonyms.

**k-anonymity**, a record is k-anonymous if its quasi-identifiers are shared by
at least *k−1* others. `pipeline/privacy/` is an OLA-style generalization
lattice solver for this, invoked by adding a `privacy_model:` block to a
config; no bundled profile ships one, so using it requires authoring a
custom config.

**Match**, the selector half of a rule; chooses which fields an action applies
to.

**NLP scrubbing**, detecting PHI entities in free text (Presidio + spaCy) and
replacing them; runs as the always-on NLP microservice.

**Output gate**, the validation barrier that scans output for residual PII and
score-gate violations and **blocks** release on a critical finding. See
[Scoring](../explanation/scoring-system.md).

**PHI / PII**, Protected Health Information / Personally Identifiable
Information.

**Priority**, integer ordering key for rules; lower numbers run first
(default 100).

**Pseudonymization**, replacing an identifier with a pseudonym that can be
reversed under controlled conditions (vs. irreversible hashing).

**Quasi-identifier (QI)**, a field that is not unique alone but can
re-identify in combination (age, ZIP, sex).

**RBAC**, role-based access control; roles are `admin`, `analyst`, `viewer`.

**Rule**, a `match → action` pair, the unit of de-identification.
See [Rules](./rules.md).

**Scoring**, the privacy × utility × quality evaluation of de-identified
output. See [Scoring system](../explanation/scoring-system.md).

**Staging layer**, optional two-phase PostgreSQL staging used by bulk/
k-anonymity jobs (`MEDANON_STAGING_DB_URL`).

**TTP**, Trusted Third Party; the role gPAS plays for pseudonym custody.
