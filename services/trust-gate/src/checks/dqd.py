"""OHDSI DQD-style checks over the OMOP CDM (Phase 3).

Structural checks (required tables, NOT NULL required fields, FK integrity, date
well-formedness) are derived from the CDM spec (``cdm/omop_model.py``). Value-range
and completeness checks are data-driven from ``config/dqd_checks.yaml``. Every
check is emitted as a :class:`CheckResult` tagged into the same Kahn categories,
dimensions, and phases as the FHIR engine, so scoring/scorecard/decision reuse
unchanged.

All checks here are deterministic (no random/batch-relative statistics), so they
all drive the verdict (the statistical advisory lives in Phase 3B / plausibility).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import yaml

from cdm.omop_model import CORE_TABLES, REQUIRED_TABLES, OmopData
from passport import CheckResult
from phases import (
    COMPLETENESS_CORE,
    COMPLETENESS_RICHNESS,
    REFERENTIAL,
    STRUCTURAL,
    VALUE_PLAUSIBILITY,
)

_log = logging.getLogger("trust_gate.dqd")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _config_path() -> str:
    env = os.environ.get("TRUST_GATE_CHECKS_PATH", "").strip()
    base = os.path.dirname(os.path.abspath(env)) if env else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "config"
    )
    return os.path.join(os.path.normpath(base), "dqd_checks.yaml")


def load_dqd_value_checks() -> dict:
    path = _config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        _log.warning("dqd_checks.yaml not found at %s", path)
        return {}


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_iso_date(value) -> bool:
    return isinstance(value, str) and bool(_ISO_DATE.match(value))


def _ck(check_id, category, subcategory, dimension, phase, applicable, violations,
        *, threshold=0.0, critical=False, description="", recommendation="") -> CheckResult:
    c = CheckResult(
        check_id=check_id,
        category=category,
        subcategory=subcategory,
        context="verification",
        applicable=applicable,
        violations=violations,
        threshold=threshold,
        critical=critical,
        description=description,
        recommendation=recommendation,
    )
    c.phase = phase
    c.dimension = dimension
    return c


def run_dqd_checks(omop: OmopData, value_checks: dict | None = None) -> list[CheckResult]:
    """Run the OMOP DQD check suite, returning a list of CheckResults."""
    value_checks = value_checks or {}
    checks: list[CheckResult] = []

    # 1. Required tables present (table level).
    for t in REQUIRED_TABLES:
        present = bool(omop.rows(t))
        checks.append(_ck(
            f"dqd.table.{t}.present", "conformance", "relational", "integrity",
            STRUCTURAL, applicable=1, violations=0 if present else 1,
            critical=True, description=f"OMOP table '{t}' is present",
            recommendation=f"Provide the required '{t}' table",
        ))

    # 2. Field/relational checks for each present core table.
    for tname in omop.present_tables():
        spec = CORE_TABLES[tname]
        rows = omop.rows(tname)
        n = len(rows)

        for col in spec.required:
            missing = sum(1 for r in rows if _is_blank(r.get(col)))
            checks.append(_ck(
                f"dqd.{tname}.{col}.not_null", "completeness", "completeness",
                "completeness", COMPLETENESS_CORE, applicable=n, violations=missing,
                description=f"{tname}.{col} is populated",
                recommendation=f"Populate required field {tname}.{col}",
            ))

        for col, ref in spec.fks:
            ref_pks = omop.pk_set(ref)
            applicable = sum(1 for r in rows if not _is_blank(r.get(col)))
            dangling = sum(
                1 for r in rows
                if not _is_blank(r.get(col)) and r.get(col) not in ref_pks
            )
            checks.append(_ck(
                f"dqd.{tname}.{col}.fk", "conformance", "relational", "integrity",
                REFERENTIAL, applicable=applicable, violations=dangling,
                description=f"{tname}.{col} references an existing {ref}",
                recommendation=f"Ensure {tname}.{col} matches a {ref} key",
            ))

        for col in spec.dates:
            applicable = sum(1 for r in rows if not _is_blank(r.get(col)))
            bad = sum(
                1 for r in rows
                if not _is_blank(r.get(col)) and not _is_iso_date(r.get(col))
            )
            checks.append(_ck(
                f"dqd.{tname}.{col}.date_format", "conformance", "value",
                "conformity", STRUCTURAL, applicable=applicable, violations=bad,
                description=f"{tname}.{col} is an ISO date",
                recommendation=f"Use ISO (YYYY-MM-DD) dates for {tname}.{col}",
            ))

    # 3. Value-range plausibility (data-driven).
    current_year = datetime.now(timezone.utc).year
    for vr in value_checks.get("value_ranges", []) or []:
        tname, col = vr.get("table"), vr.get("column")
        if tname not in CORE_TABLES or not omop.rows(tname):
            continue
        lo = vr.get("min")
        hi = vr.get("max")
        if hi is None and col == "year_of_birth":
            hi = current_year
        rows = omop.rows(tname)
        applicable = 0
        violations = 0
        details: list[dict] = []
        for r in rows:
            v = r.get(col)
            if not isinstance(v, (int, float)):
                continue
            applicable += 1
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                violations += 1
                details.append({
                    "resource_type": tname,
                    "resource_id": str(r.get(f"{tname}_id", ""))[:64],
                    "path": col,
                    "value": v,
                    "detail": f"{col}={v} is outside the plausible range [{lo}, {hi}]",
                })
        ck = _ck(
            f"dqd.{tname}.{col}.value_range", "plausibility", "atemporal",
            "accuracy", VALUE_PLAUSIBILITY, applicable=applicable, violations=violations,
            description=f"{tname}.{col} within [{lo}, {hi}]",
            recommendation=f"Investigate out-of-range {tname}.{col} values",
        )
        for d in details[:50]:
            ck.add_detail(**d)
        checks.append(ck)

    # 4. Recommended-field completeness / richness (data-driven).
    for comp in value_checks.get("completeness", []) or []:
        tname, col = comp.get("table"), comp.get("column")
        if tname not in CORE_TABLES or not omop.rows(tname):
            continue
        rows = omop.rows(tname)
        n = len(rows)
        missing = sum(1 for r in rows if _is_blank(r.get(col)))
        min_fill = float(comp.get("min_fill", 0.5))
        checks.append(_ck(
            f"dqd.{tname}.{col}.completeness", "completeness", "completeness",
            "completeness", COMPLETENESS_RICHNESS, applicable=n, violations=missing,
            threshold=max(0.0, 1.0 - min_fill),
            description=f"{tname}.{col} populated in >= {int(min_fill * 100)}% of rows",
            recommendation=f"Improve population of {tname}.{col}",
        ))

    return checks
