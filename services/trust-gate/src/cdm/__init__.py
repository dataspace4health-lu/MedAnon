"""OMOP CDM normalization for the Trust Gate platform (Phase 3).

Provider data arrives in many shapes (FHIR, SQL/tabular). Normalizing onto a
common model (OMOP CDM v5.4 core subset) lets the same OHDSI-DQD-style checks run
portably over any source, which is the dominant remedy for cross-institution
heterogeneity (JMIR 2025 lifecycle review; OHDSI).

This is a lightweight, in-memory representation (not a database): just enough
structure for the field/concept-level checks in ``checks/dqd.py``.
"""

from cdm.omop_model import CORE_TABLES, OmopData

__all__ = ["CORE_TABLES", "OmopData"]
