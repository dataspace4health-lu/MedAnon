"""Timeliness / currency checks (DAMA timeliness; Kahn plausibility/temporal).

Two opt-in measures, both NA until the operator declares what "timely" means for
their use (no invented thresholds):

  - record_lag — event→record latency: a clinical event recorded far later than it
    occurred is a data-capture-lag defect. Configured by
    ``TRUST_GATE_MAX_RECORD_LAG_DAYS``; NA when unset or dates absent.
  - currency   — freshness vs the dataset's extraction time: a resource whose
    ``meta.lastUpdated`` is older than ``TRUST_GATE_CURRENCY_WINDOW_DAYS`` before
    ``provenance.extraction_time`` is stale. NA when unset or no extraction time.

Both carry the *currency* DQ dimension (assigned by the engine), so the scorecard
gets a real currency signal beyond the always-on not-in-future rules.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os

from constants import threshold_for
from passport import CheckResult

_log = logging.getLogger("trust_gate.checks.timeliness")

_EVENT_DATE_FIELDS = (
    "effectiveDateTime",
    "occurrenceDateTime",
    "performedDateTime",
    "onsetDateTime",
    "issued",
)


def _to_date(value) -> _dt.date | None:
    """Parse a FHIR date/dateTime to a date (date-granularity), or None."""
    if not isinstance(value, str) or len(value) < 4:
        return None
    parts = value[:10].split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return _dt.date(year, month, day)
    except (ValueError, IndexError):
        return None


def _event_date(res: dict):
    for f in _EVENT_DATE_FIELDS:
        if isinstance(res.get(f), str):
            return res[f]
    return None


def _record_date(res: dict):
    rd = res.get("recordedDate")
    if isinstance(rd, str):
        return rd
    meta = res.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("lastUpdated"), str):
        return meta["lastUpdated"]
    return None


def _int_env(name: str):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        _log.warning("%s is not an integer (%r) — ignored", name, raw)
        return None


def evaluate(
    resources: list[dict],
    thresholds: dict[str, float] | None = None,
    *,
    extraction_time: str | None = None,
) -> list[CheckResult]:
    max_lag = _int_env("TRUST_GATE_MAX_RECORD_LAG_DAYS")
    window = _int_env("TRUST_GATE_CURRENCY_WINDOW_DAYS")

    lag = CheckResult(
        check_id="plausibility.record_lag",
        category="plausibility",
        subcategory="temporal",
        context="verification",
        threshold=threshold_for("plausibility.record_lag", thresholds),
        description="Clinical events are recorded within an acceptable lag of when they occurred.",
        recommendation="Investigate large event→record latency (data-capture lag).",
        hdqt_category="plausibility",
        hdqt_dimension="temporally_implausible",
    )
    currency = CheckResult(
        check_id="plausibility.currency",
        category="plausibility",
        subcategory="temporal",
        context="verification",
        threshold=threshold_for("plausibility.currency", thresholds),
        description="Resources are fresh relative to the dataset extraction time.",
        recommendation="Refresh stale records or widen the currency window for this use.",
        hdqt_category="plausibility",
        hdqt_dimension="temporally_implausible",
    )

    ext_date = _to_date(extraction_time)
    for res in resources:
        if not isinstance(res, dict):
            continue
        if max_lag is not None:
            ev, rec = _to_date(_event_date(res)), _to_date(_record_date(res))
            if ev is not None and rec is not None:
                lag.applicable += 1
                if (rec - ev).days > max_lag:
                    lag.violations += 1
                    lag.add_detail(
                        resource_type=res.get("resourceType"),
                        resource_id=res.get("id", ""),
                        path="recordedDate/meta.lastUpdated",
                        detail=f"record lag {(rec - ev).days}d exceeds {max_lag}d",
                    )
        if window is not None and ext_date is not None:
            meta = res.get("meta")
            lu = _to_date(meta.get("lastUpdated")) if isinstance(meta, dict) else None
            if lu is not None:
                currency.applicable += 1
                if (ext_date - lu).days > window:
                    currency.violations += 1
                    currency.add_detail(
                        resource_type=res.get("resourceType"),
                        resource_id=res.get("id", ""),
                        path="meta.lastUpdated",
                        detail=f"stale by {(ext_date - lu).days}d (> {window}d window)",
                    )
    return [lag, currency]
