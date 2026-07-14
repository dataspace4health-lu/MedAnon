"""Cumulative-exposure analysis across releases (TEHDAS2 D7.2 §5.5.7).

A single release can be k-anonymous yet still leak once you account for *other*
releases of overlapping populations: publishing the same individuals under
different generalisations enables a **differencing / linkage attack** (Dwork &
Roth 2014 §1; Sweeney 2002). D7.2 §5.5.7 asks holders to consider cumulative
disclosure over repeated releases, not just the release in hand.

This module gives the measurement half:

- :func:`fingerprint_population` reduces a release's subject ids to a set of
  **keyed one-way hashes** — PII-safe (HMAC over already-pseudonymous ids, so
  nothing reversible or linkable outside the system is stored), yet still
  supporting set-overlap between releases.
- :func:`assess_cumulative_exposure` compares a new release's fingerprint to
  prior releases (same permit / recipient) and reports repeat exposure, cohort
  overlap, and — the risk that actually matters — individuals re-released under a
  *different* quasi-identifier signature (the differencing surface), with a
  graded verdict.

The durable side (recording releases, loading priors) lives in
``integrations/postgres/release_ledger``; this module is pure and I/O-free.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

# Default thresholds (fractions of the new release's population). These are not
# regulatory constants — the literature gives no single number — but encode the
# differencing rationale: any reuse under a *different* generalisation is worth a
# look (review), and substantial such reuse is elevated risk. Tunable per call.
DEFAULT_REVIEW_OVERLAP = 0.5
DEFAULT_ELEVATED_DIFFERENCING = 0.5


def fingerprint_population(subject_ids: list[str], key: str) -> list[str]:
    """Keyed one-way fingerprint of a release population (sorted, de-duplicated).

    Each id becomes ``HMAC-SHA256(key, id)``; the salted digest set supports
    overlap comparison without persisting the ids themselves. ``key`` should be a
    server-side secret (e.g. ``MEDANON_HASH_KEY``) so digests are neither
    reversible nor linkable across deployments.
    """
    if not key:
        raise ValueError("a non-empty key is required to fingerprint a population")
    kb = key.encode()
    digests = {
        hmac.new(kb, str(sid).encode(), hashlib.sha256).hexdigest()
        for sid in subject_ids
        if str(sid).strip()
    }
    return sorted(digests)


def _signature(qi_signature: Any) -> str:
    """Normalise a QI signature (list of paths or string) to a stable key."""
    if isinstance(qi_signature, (list, tuple)):
        return "|".join(sorted(str(x) for x in qi_signature))
    return str(qi_signature or "")


def assess_cumulative_exposure(
    new_fingerprint: list[str],
    prior_releases: list[dict],
    *,
    qi_signature: Any = "",
    review_overlap: float = DEFAULT_REVIEW_OVERLAP,
    elevated_differencing: float = DEFAULT_ELEVATED_DIFFERENCING,
) -> dict[str, Any]:
    """Assess a new release against prior releases to the same recipient.

    Args:
        new_fingerprint: output of :func:`fingerprint_population` for this release.
        prior_releases: list of ``{"release_id", "fingerprint": [...],
            "qi_signature", "created_at"?}`` for earlier releases under the same
            permit / recipient.
        qi_signature: the new release's QI signature (paths + granularity). Overlap
            with a prior release carrying a *different* signature is the
            differencing surface.
        review_overlap: cohort-overlap fraction at/above which a same-signature
            release is flagged for review.
        elevated_differencing: differencing fraction at/above which the release is
            elevated risk.

    Returns:
        Anonymous stats + graded ``verdict`` (``clear`` / ``review`` / ``elevated``).
    """
    new_set = set(new_fingerprint)
    n = len(new_set)
    new_sig = _signature(qi_signature)

    if n == 0 or not prior_releases:
        return {
            "verdict": "clear",
            "population": n,
            "prior_releases": len(prior_releases),
            "max_overlap_fraction": 0.0,
            "overlapping_individuals": 0,
            "differencing_individuals": 0,
            "differencing_fraction": 0.0,
            "max_repeat_count": 0,
            "reason": "no population or no prior releases",
        }

    repeat_count: dict[str, int] = {}
    differencing: set[str] = set()
    max_overlap = 0.0
    overlaps: list[dict[str, Any]] = []

    for prior in prior_releases:
        prior_set = set(prior.get("fingerprint") or [])
        inter = new_set & prior_set
        if not inter:
            continue
        frac = len(inter) / n
        max_overlap = max(max_overlap, frac)
        prior_sig = _signature(prior.get("qi_signature"))
        differs = prior_sig != new_sig
        if differs:
            differencing |= inter
        for h in inter:
            repeat_count[h] = repeat_count.get(h, 0) + 1
        overlaps.append(
            {
                "release_id": prior.get("release_id"),
                "overlap_fraction": round(frac, 6),
                "different_qi_signature": differs,
            }
        )

    overlapping = len(repeat_count)
    diff_count = len(differencing)
    diff_frac = diff_count / n
    max_repeat = max(repeat_count.values(), default=0)

    if diff_frac >= elevated_differencing:
        verdict = "elevated"
        reason = (
            f"{diff_count}/{n} individuals re-released under a different QI "
            "signature (differencing/linkage surface)"
        )
    elif diff_count > 0 or max_overlap >= review_overlap:
        verdict = "review"
        reason = "cohort reuse across releases" + (
            " with differing generalisation" if diff_count else ""
        )
    else:
        verdict = "clear"
        reason = "overlap within same-signature tolerance"

    return {
        "verdict": verdict,
        "population": n,
        "prior_releases": len(prior_releases),
        "max_overlap_fraction": round(max_overlap, 6),
        "overlapping_individuals": overlapping,
        "differencing_individuals": diff_count,
        "differencing_fraction": round(diff_frac, 6),
        "max_repeat_count": max_repeat,
        "overlaps": overlaps,
        "reason": reason,
    }
