"""Opt-out exclusion (EHDS Art 71 / D7.2 §4.7, Annex 6).

Data subjects may opt out of secondary use for *new* projects (Art 71(3):
opt-out does not apply to ongoing projects). Both D7.2 Annex-6 worked
minimisation scenarios list "the HDAB asks the national contact point for
opt-outs and removes them from the datasets" as a standard data-preparation
step, performed *before* de-identification so an opted-out subject's records
never reach gPAS/NLP.

This module is the reusable exclusion engine: it resolves the opt-out set
(request-supplied ids + a configured file source), matches resources to
subjects (Patient by id/identifier; linked resources by subject/patient
reference), and drops the matches — emitting a PHI-safe ``optout.excluded``
audit event (count only, never the ids). In regulated mode it fails **closed**:
if an opt-out source is configured but cannot be read, processing is refused
rather than silently releasing opted-out subjects.

Design: matching is on the *original* subject identifier (pre-pseudonymisation).
The opt-out set therefore holds raw Patient ids / identifier values, exactly as
a national opt-out register would supply them.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.exclusion")

# Env: newline-delimited file of opted-out subject identifiers (one per line;
# blank lines and ``#`` comments ignored). A DB-backed source can be added
# later behind the same ``load_optout_set`` seam.
_OPTOUT_FILE_ENV = "MEDANON_OPTOUT_FILE"


class OptOutSourceError(RuntimeError):
    """Raised (regulated mode) when a configured opt-out source cannot be read."""


def optout_configured() -> bool:
    """True when any opt-out source (file) is configured.

    Request-supplied ids alone do not count as "configured" — they are an
    explicit per-request list the caller opted into; the fail-closed guarantee
    is about a *configured* register being unreachable.
    """
    return bool(os.environ.get(_OPTOUT_FILE_ENV, "").strip())


def load_optout_set(extra_ids: list[str] | None = None) -> set[str]:
    """Resolve the opt-out set from the configured file source + *extra_ids*.

    Raises :class:`OptOutSourceError` in regulated mode when the configured
    file exists in config but cannot be read (fail-closed). Outside regulated
    mode an unreadable source logs a warning and contributes nothing
    (best-effort), so a transient FS issue does not hard-fail non-regulated
    throughput pipelines.
    """
    ids: set[str] = {str(x).strip() for x in (extra_ids or []) if str(x).strip()}

    path = os.environ.get(_OPTOUT_FILE_ENV, "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s and not s.startswith("#"):
                        ids.add(s)
        except OSError as exc:
            from utils.regulated import regulated_mode

            if regulated_mode():
                raise OptOutSourceError(
                    f"MEDANON_REGULATED_MODE is on and the opt-out source "
                    f"{path!r} could not be read ({type(exc).__name__}); refusing "
                    "to process — opted-out subjects (Art 71) might otherwise be "
                    "released."
                ) from exc
            _log.warning(
                "optout_source_unreadable path=%s error=%s — proceeding without "
                "it (non-regulated best-effort)",
                path,
                type(exc).__name__,
            )
    return ids


def subject_keys(resource: dict) -> set[str]:
    """Return the set of subject identifiers *resource* could match on.

    A Patient matches on its ``id`` and any ``identifier.value``. A linked
    resource (Observation, Condition, Encounter, …) matches on the bare id of
    its ``subject``/``patient`` reference (``Patient/abc`` → ``abc``, and the
    full reference string too, so either form in the opt-out register hits).
    """
    keys: set[str] = set()
    rtype = resource.get("resourceType", "")

    if rtype == "Patient":
        rid = resource.get("id")
        if rid:
            keys.add(str(rid))
        for ident in resource.get("identifier") or []:
            if isinstance(ident, dict):
                val = ident.get("value")
                if val:
                    keys.add(str(val))
        return keys

    for ref_field in ("subject", "patient"):
        ref = resource.get(ref_field)
        if isinstance(ref, dict):
            ref_val = ref.get("reference")
            if ref_val:
                ref_str = str(ref_val)
                keys.add(ref_str)
                keys.add(ref_str.split("/")[-1])  # bare id form
    return keys


def filter_excluded(
    resources: list[dict], optout_set: set[str]
) -> tuple[list[dict], int]:
    """Drop resources belonging to opted-out subjects (Patient + linked).

    Returns ``(kept, excluded_count)``. A no-op (returns the input list
    object unchanged) when *optout_set* is empty, so the common
    nothing-configured path is zero-cost.

    Two passes, because a national opt-out register keys on the patient's
    NHS/MRN **identifier**, while that patient's Observations/Conditions/
    Encounters reference them by FHIR **resource id** (``Patient/{id}``):

    1. Find every Patient whose id/identifier is in *optout_set* and expand
       the set with that Patient's resource id (bare + ``Patient/{id}`` form).
    2. Drop every resource — Patient and linked — matching the expanded set,
       so an identifier-only opt-out still removes the whole subject rather
       than orphaning their clinical resources.
    """
    if not optout_set:
        return resources, 0

    # Pass 1: bridge identifier-match → resource-id so linked resources match.
    expanded = set(optout_set)
    for r in resources:
        if (
            isinstance(r, dict)
            and r.get("resourceType") == "Patient"
            and subject_keys(r) & optout_set
        ):
            rid = r.get("id")
            if rid:
                expanded.add(str(rid))
                expanded.add(f"Patient/{rid}")

    # Pass 2: drop Patient + linked resources of every opted-out subject.
    kept: list[dict] = []
    excluded = 0
    for r in resources:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        if subject_keys(r) & expanded:
            excluded += 1
        else:
            kept.append(r)
    return kept, excluded


def apply_optout(
    resources: list[dict],
    *,
    extra_ids: list[str] | None = None,
    actor: str = "system",
    dataset_id: str = "",
) -> tuple[list[dict], int]:
    """Convenience: resolve the opt-out set, filter, and audit in one call.

    Returns ``(kept, excluded_count)``. Emits a PHI-safe ``optout.excluded``
    audit event (count + dataset id only, never subject ids) when anything was
    excluded. Fails closed in regulated mode via :func:`load_optout_set` when a
    configured source is unreachable.

    When no source is configured and no *extra_ids* are supplied, this is a
    cheap no-op returning the input unchanged.
    """
    if not optout_configured() and not extra_ids:
        return resources, 0

    optout_set = load_optout_set(extra_ids)
    kept, excluded = filter_excluded(resources, optout_set)

    if excluded:
        try:
            from utils.audit import emit as audit_emit

            audit_emit(
                "optout.excluded",
                actor=actor,
                action="data_minimisation",
                outcome="success",
                detail={"excluded_count": excluded, "dataset_id": dataset_id},
            )
        except Exception:  # noqa: BLE001 — audit must never break the pipeline
            pass
        _log.info("optout_excluded count=%d dataset=%s", excluded, dataset_id or "-")
    return kept, excluded
