"""Typed exception hierarchy for the de-identification pipeline.

Replaces the historical pattern of bare ``except Exception:`` blocks that
collapsed *every* failure — including programming bugs — into a silent
redaction.  With typed exceptions, the correction stage can apply a policy
per error *kind* and, crucially, a bug in an action (e.g. a ``generalize``
defect) surfaces as a distinct, observable event instead of masquerading as a
deliberate redaction.

Hierarchy
---------
``DeidError`` is the root.  Catch it to handle any *expected* de-identification
failure; let everything else (``AttributeError``, ``TypeError``, ``KeyError``
from a genuine bug) propagate to the job-level error boundary.

    DeidError
     ├─ ActionError          — a transformation action failed
     │   ├─ GeneralizeError
     │   └─ EncryptError
     ├─ FhirPathError        — FHIRPath compile / node navigation failed
     │   └─ FallbackRedactError  — the safety-fallback redact itself failed
     ├─ NlpError             — NLP detection / replacement failed
     │   ├─ NlpUnavailableError   — the NLP service is unreachable / disabled
     │   └─ NlpSpanError          — a detected (start, end) span was malformed
     └─ NormalizationError   — a source adapter failed to parse/serialize input

These are deliberately lightweight (no behavior beyond ``Exception``); their
value is the *type*, which lets call sites and the correction stage discriminate
recoverable, fail-closed conditions from unexpected defects.
"""

from __future__ import annotations


class DeidError(Exception):
    """Base class for all expected de-identification failures."""


class ActionError(DeidError):
    """A de-identification action raised while transforming an element."""


class GeneralizeError(ActionError):
    """The ``generalize`` action failed (e.g. unknown strategy, bad value)."""


class EncryptError(ActionError):
    """The ``encrypt`` / ``decrypt`` action failed (e.g. key error)."""


class FhirPathError(DeidError):
    """FHIRPath compilation or node navigation failed."""


class FallbackRedactError(FhirPathError):
    """The safety-fallback redact after a FHIRPath evaluation failure failed too.

    Raised in "skip" mode so the processor quarantines the whole resource: an
    element we can neither evaluate nor redact must never be emitted. Without
    this signal the dispatcher would continue and the matched element would
    pass through unprocessed.
    """


class NlpError(DeidError):
    """NLP detection or replacement failed."""


class NlpUnavailableError(NlpError, RuntimeError):
    """The NLP service is unreachable or disabled.

    This is the fail-closed signal: callers redact the affected field rather
    than pass the original text through.

    Also subclasses ``RuntimeError`` so existing ``except RuntimeError``
    handlers (and tests) that predate the typed hierarchy keep working — the
    historical contract was a bare ``RuntimeError`` for this condition.
    """


class NlpSpanError(NlpError):
    """A detected NLP span ``(start, end)`` was malformed or out of bounds."""


class NormalizationError(DeidError):
    """A source-format adapter failed to parse or serialize input.

    Raised by the (future) normalization layer for CDA / HL7v2 / DICOM /
    tabular adapters; replaces the bare ``ValueError`` raised today by the
    standalone ``formats/*`` scrubbers.
    """


class OutputBlocked(Exception):
    """The output-validation barrier refused to release a batch.

    Deliberately *not* a ``DeidError``: nothing failed to de-identify.  The
    barrier evaluated the result and withheld it.  Callers must translate this
    into a refusal (HTTP 422, ``job.error``), never into a partial release.

    Lives here rather than in :mod:`pipeline.validation` so that
    :class:`pipeline.gate.PiiLeakError` can subclass it without an import cycle
    (``validation`` imports ``gate``).  ``str(exc)`` is the complete
    plain-language feedback; ``reasons`` is the machine-readable breakdown.
    """

    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        self.reasons = reasons or []
        super().__init__(message)
