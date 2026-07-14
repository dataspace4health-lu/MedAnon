"""Statistical Disclosure Control / export-decision workflow (D7.2 §5.6, Five Safes).

Output checking for a Secure Processing Environment: before a de-identified or
synthetic dataset leaves the SPE, an export decision (``approve`` / ``refer`` /
``refuse``) is made from the privacy-risk assessment (WS1) and minimisation
report (WS2), and recorded so it can be carried in the Transformation Passport
(``disclosure`` section) and audited.
"""

from pipeline.disclosure.decision import assess_export_decision, APPROVE, REFER, REFUSE

__all__ = ["assess_export_decision", "APPROVE", "REFER", "REFUSE"]
