---
title: "Actions"
sidebar_position: 4
description: "What each de-identification action does and when to use it."
---

# Actions

Actions are the verbs of a rule's `action:` key. Each lives in its own module
under `actions/`. Parameters and defaults are listed in
[Rules › Actions and parameters](./rules.md#actions-and-parameters); this page
explains *behaviour and intent*.

## Suppression & replacement

| Action | What it does | Use for |
|---|---|---|
| `redact` | Replaces the value with a fixed string (default `[REDACTED]`). | Free-form identifiers you do not need to keep |
| `mask` | Keeps part of the value, masks the rest (5 strategies: keep prefix/suffix/domain/country-code, or full). | Phone numbers, emails, MRNs where a partial value aids QA |
| `scrub_text` | Regex-based removal of identifier patterns from free text. | Narrative fields without NLP |

## Generalization

| Action | What it does | Use for |
|---|---|---|
| `generalize` | Coarsens a value: dates → year / year-month / decade, ages → bracket, ZIP → prefix, numbers → rounded, categories → mapping. | Reducing precision to lower re-identification risk while keeping analytic value |
| `perturb` | Adds bounded random noise within `[min, max]`. | Numeric values where exactness is not required |

## Cryptographic & reversible

| Action | What it does | Use for |
|---|---|---|
| `cryptohash` | HMAC-SHA3-256 (keyed) hash, deterministic, one-way. | Linking records by a stable token without storing the original |
| `encrypt` / `decrypt` | RSA encrypt/decrypt of a value. | Values that must be recoverable by a key holder |
| `tokenize` | Format-preserving token (optionally per-`namespace`, length-preserving). | Replacing IDs while keeping downstream format checks happy |
| `date_shift` | Deterministic per-subject date offset (preserves intervals; optional `anchor_path`, age-bracket preservation). | Shifting all of a subject's dates consistently |
| `substitute` | Replaces with a supplied fixed/surrogate value (`substitute_with`). | Swapping a value for a known placeholder |

## Pseudonymization (gPAS TTP)

| Action | What it does | Use for |
|---|---|---|
| `gpas_pseudonymize` | Requests a reversible pseudonym from gPAS for the matched value. | Patient/visit IDs that must be re-linkable under controlled conditions |
| `gpas_depseudonymize` | Resolves a pseudonym back to the original (controlled). | Adverse-event investigation, authorized re-identification |

Requires a reachable gPAS (`GPAS_URL`, `GPAS_DOMAIN`, …). See
[Configuration](./configuration.md#gpas-pseudonymization).

## NLP free-text scrubbing

| Action | What it does | Use for |
|---|---|---|
| `nlp_scrub` / `nlp_detect` | Detects PHI entities in free text (Presidio + spaCy via the NLP microservice) and replaces them. Honours `entities`, `threshold`, `entity_actions`, `html`, `base64_encoded`. | Clinical narratives, attachment text |
| `nlp_detect_act` | Entity-specific conditional NLP, apply different actions per detected entity type. | Fine-grained narrative handling |

NLP **fails closed**: if the microservice is unreachable the text is replaced
with `[NLP_UNAVAILABLE]` rather than leaking unscrubbed content (configurable via
`fail_mode`).
