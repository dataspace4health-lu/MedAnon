"""Kahn-aligned data-quality checks.

Each module produces ``CheckResult`` objects in one Kahn category:
  - ``conformance``  — structural validity, profiles, coding, terminology, refs
  - ``completeness`` — required elements, value-or-dataAbsentReason
  - ``plausibility`` — uniqueness + the declarative clinical-plausibility rule pack

``governance`` produces auditability evidence (a fitness-for-use policy signal,
not a Kahn data-quality category).
"""
