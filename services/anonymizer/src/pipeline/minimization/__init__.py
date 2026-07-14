"""Data minimisation assessment (TEHDAS2 D7.2 §3).

Evaluate-and-recommend, not transform: this package reads a dataset, classifies
direct vs quasi identifiers, recommends lower-granularity representations for the
quasi-identifiers (age bands, year/month instead of exact dates, ZIP prefixes),
and flags variables not justified by a declared purpose. The actual
transformation stays in the rule/lattice pipeline; this produces the
Minimisation Report that feeds the Transformation Passport (§5.5.1).
"""

from pipeline.minimization.report import assess_minimisation

__all__ = ["assess_minimisation"]
