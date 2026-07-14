"""Verdict layer: turn the raw check results into the passport verdict.

Extracted from the former monolithic engine.py so each concern is one module:
  - scoring    category/overall roll-up, dimension scorecard, grades, advisory.
  - decision   the PASS/CONDITIONAL/BLOCK policy + per-resource-type thresholds.
  - coverage   assessment-coverage transparency (what actually ran).
  - fitness    purpose-bound fitness-for-use lists + statement.
  - runner     check orchestration + per-sector / per-phase sub-reports.

engine.assess() is now a thin pipeline over these. Dependency direction:
runner -> decision -> scoring; fitness -> coverage; nothing imports engine.
"""
