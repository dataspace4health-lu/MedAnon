"""Differential-privacy mechanisms and budget accounting (TEHDAS2 D7.2 §5.5.3).

D7.2 lists differential privacy as the formal privacy guarantee for aggregate
release and DP synthesis. This module is the in-house DP engine the statistical
export route (§5.5.4 "statistical format") and the DP synthesiser (§5.5.5) build
on. It is deliberately standard-library-only, matching the rest of the
``analytics`` package, so it always runs (``diffprivlib`` is torch-free but
pins older transitive deps and is *not* a hard requirement here).

What it provides
----------------
- **Laplace mechanism** for pure ``epsilon``-DP on queries with bounded L1
  sensitivity (Dwork & Roth 2014, Def 3.3 / Thm 3.6).
- **Analytic Gaussian mechanism** for ``(epsilon, delta)``-DP on L2-bounded
  queries. Unlike the classic ``sigma >= sqrt(2 ln(1.25/delta)) * GS / epsilon``
  bound (which is loose and only valid for ``epsilon <= 1``), this calibrates the
  *exact minimal* sigma for any ``epsilon`` via Balle & Wang, ICML 2018,
  "Improving the Gaussian Mechanism for Differential Privacy".
- **Budget accountant** using basic sequential composition (Dwork & Roth 2014,
  Thm 3.16): total spend is the sum of per-query ``(epsilon, delta)``. Basic
  composition is the conservative (fail-closed) choice; it over-charges relative
  to advanced/RDP accounting, which is the safe direction for a privacy barrier.
- **Histogram / count** helpers that apply the Laplace mechanism at the correct
  sensitivity for the chosen neighbouring-dataset model.

Neighbouring-dataset model
--------------------------
Sensitivity depends on what "one individual" changes. We expose both:

- **unbounded** DP (add/remove one record): a histogram over disjoint bins has
  L1 sensitivity **1** (removing a record decrements exactly one bin).
- **bounded** DP (change one record, dataset size fixed): L1 sensitivity **2**
  (one bin ``-1``, another ``+1``).

The default is *unbounded* (matches "one patient in or out of the cohort").

Noise source
------------
The default noise source is :class:`secrets.SystemRandom` (CSPRNG). A seedable
:class:`random.Random` may be injected for deterministic tests. Note the
floating-point sampling caveat of Mironov 2012 ("On significance of the least
significant bits of differential privacy"): the naive inverse-CDF sampler used
here is standard and adequate for aggregate research release, but is not
hardened against timing/rounding side channels; that is documented, not hidden.
"""

from __future__ import annotations

import math
import random
import secrets
from collections.abc import Iterable, Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PrivacyBudgetExceeded(RuntimeError):
    """Raised (fail-closed) when a query would overspend the DP budget."""


# ---------------------------------------------------------------------------
# Noise sources
# ---------------------------------------------------------------------------


def _default_rng() -> random.Random:
    """A cryptographically-secure, non-seedable RNG for production noise."""
    return secrets.SystemRandom()


def laplace_noise(scale: float, rng: random.Random | None = None) -> float:
    """Draw one sample from ``Laplace(0, scale)`` via inverse-CDF.

    ``scale`` is the ``b`` parameter (``b = sensitivity / epsilon``). Uses
    ``u ~ Uniform(-0.5, 0.5)`` then ``-b * sign(u) * ln(1 - 2|u|)``.
    """
    if scale <= 0:
        raise ValueError(f"laplace scale must be > 0, got {scale}")
    r = rng or _default_rng()
    u = r.random() - 0.5
    return -scale * math.copysign(1.0, u) * math.log1p(-2.0 * abs(u))


def gaussian_noise(sigma: float, rng: random.Random | None = None) -> float:
    """Draw one sample from ``N(0, sigma^2)``."""
    if sigma <= 0:
        raise ValueError(f"gaussian sigma must be > 0, got {sigma}")
    r = rng or _default_rng()
    return r.gauss(0.0, sigma)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def laplace_scale(epsilon: float, l1_sensitivity: float = 1.0) -> float:
    """Laplace scale ``b = L1 / epsilon`` for pure ``epsilon``-DP."""
    if epsilon <= 0:
        raise ValueError(f"epsilon must be > 0, got {epsilon}")
    if l1_sensitivity <= 0:
        raise ValueError(f"l1_sensitivity must be > 0, got {l1_sensitivity}")
    return l1_sensitivity / epsilon


def _phi(t: float) -> float:
    """Standard-normal CDF via the error function (stdlib only)."""
    return 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))


def analytic_gaussian_sigma(
    epsilon: float,
    delta: float,
    l2_sensitivity: float = 1.0,
    *,
    tol: float = 1e-12,
) -> float:
    """Minimal ``sigma`` for ``(epsilon, delta)``-DP (Balle & Wang, ICML 2018).

    Solves for the exact smallest noise scale rather than the loose classic
    bound. Valid for any ``epsilon > 0`` and ``0 < delta < 1``.
    """
    if epsilon <= 0:
        raise ValueError(f"epsilon must be > 0, got {epsilon}")
    if not (0.0 < delta < 1.0):
        raise ValueError(f"delta must be in (0, 1), got {delta}")
    if l2_sensitivity <= 0:
        raise ValueError(f"l2_sensitivity must be > 0, got {l2_sensitivity}")

    def case_a(eps: float, s: float) -> float:
        return _phi(math.sqrt(eps * s)) - math.exp(eps) * _phi(
            -math.sqrt(eps * (s + 2.0))
        )

    def case_b(eps: float, s: float) -> float:
        return _phi(-math.sqrt(eps * s)) - math.exp(eps) * _phi(
            -math.sqrt(eps * (s + 2.0))
        )

    delta_thr = case_a(epsilon, 0.0)
    if abs(delta - delta_thr) <= tol:
        alpha = 1.0
    else:
        if delta > delta_thr:
            s_to_delta = lambda s: case_a(epsilon, s)  # noqa: E731
            s_to_alpha = lambda s: math.sqrt(1.0 + s / 2.0) - math.sqrt(s / 2.0)  # noqa: E731
            stop_doubling = lambda s: s_to_delta(s) >= delta  # noqa: E731
            go_left = lambda s: s_to_delta(s) > delta  # noqa: E731
        else:
            s_to_delta = lambda s: case_b(epsilon, s)  # noqa: E731
            s_to_alpha = lambda s: math.sqrt(1.0 + s / 2.0) + math.sqrt(s / 2.0)  # noqa: E731
            stop_doubling = lambda s: s_to_delta(s) <= delta  # noqa: E731
            go_left = lambda s: s_to_delta(s) < delta  # noqa: E731

        # Doubling trick to bracket the root, then bisection.
        s_inf, s_sup = 0.0, 1.0
        while not stop_doubling(s_sup):
            s_inf = s_sup
            s_sup *= 2.0
        s_mid = s_inf + (s_sup - s_inf) / 2.0
        while abs(s_to_delta(s_mid) - delta) > tol:
            if go_left(s_mid):
                s_sup = s_mid
            else:
                s_inf = s_mid
            s_mid = s_inf + (s_sup - s_inf) / 2.0
        alpha = s_to_alpha(s_mid)

    return alpha * l2_sensitivity / math.sqrt(2.0 * epsilon)


# ---------------------------------------------------------------------------
# Budget accountant (basic sequential composition)
# ---------------------------------------------------------------------------


class PrivacyAccountant:
    """Tracks DP spend against a fixed ``(epsilon, delta)`` budget.

    Basic sequential composition (Dwork & Roth 2014, Thm 3.16): the total
    privacy loss of a sequence of mechanisms is bounded by the sum of their
    ``epsilon`` and the sum of their ``delta``. :meth:`spend` is fail-closed:
    it raises :class:`PrivacyBudgetExceeded` *before* recording an over-budget
    query, so no noise is ever released beyond the declared guarantee.
    """

    def __init__(self, epsilon: float, delta: float = 0.0) -> None:
        if epsilon <= 0:
            raise ValueError(f"budget epsilon must be > 0, got {epsilon}")
        if delta < 0:
            raise ValueError(f"budget delta must be >= 0, got {delta}")
        self.total_epsilon = float(epsilon)
        self.total_delta = float(delta)
        self.spent_epsilon = 0.0
        self.spent_delta = 0.0
        self.ledger: list[dict[str, Any]] = []

    @property
    def remaining_epsilon(self) -> float:
        return self.total_epsilon - self.spent_epsilon

    @property
    def remaining_delta(self) -> float:
        return self.total_delta - self.spent_delta

    def spend(
        self, epsilon: float, delta: float = 0.0, *, label: str = "query"
    ) -> None:
        """Charge ``(epsilon, delta)`` to the budget or fail closed."""
        if epsilon <= 0:
            raise ValueError(f"query epsilon must be > 0, got {epsilon}")
        if delta < 0:
            raise ValueError(f"query delta must be >= 0, got {delta}")
        # Tolerance guards against float drift when spending in equal fractions.
        if self.spent_epsilon + epsilon > self.total_epsilon + 1e-12 or (
            self.spent_delta + delta > self.total_delta + 1e-12
        ):
            raise PrivacyBudgetExceeded(
                f"'{label}' needs (eps={epsilon:g}, delta={delta:g}) but only "
                f"(eps={self.remaining_epsilon:g}, delta={self.remaining_delta:g}) remain"
            )
        self.spent_epsilon += epsilon
        self.spent_delta += delta
        self.ledger.append({"label": label, "epsilon": epsilon, "delta": delta})

    def summary(self) -> dict[str, Any]:
        """Anonymous accounting summary for a passport (no data, only budgets)."""
        return {
            "mechanism": "basic_sequential_composition",
            "reference": "Dwork & Roth 2014, Thm 3.16",
            "total": {"epsilon": self.total_epsilon, "delta": self.total_delta},
            "spent": {
                "epsilon": round(self.spent_epsilon, 6),
                "delta": round(self.spent_delta, 6),
            },
            "remaining": {
                "epsilon": round(self.remaining_epsilon, 6),
                "delta": round(self.remaining_delta, 6),
            },
            "queries": list(self.ledger),
        }


# ---------------------------------------------------------------------------
# Aggregate queries
# ---------------------------------------------------------------------------


def histogram_l1_sensitivity(*, unbounded: bool = True) -> float:
    """L1 sensitivity of a disjoint-bin histogram under the neighbour model.

    unbounded (add/remove one record) -> 1; bounded (change one record) -> 2.
    """
    return 1.0 if unbounded else 2.0


def dp_histogram(
    counts: Mapping[Any, int] | Iterable[tuple[Any, int]],
    epsilon: float,
    *,
    unbounded: bool = True,
    non_negative: bool = True,
    accountant: PrivacyAccountant | None = None,
    label: str = "histogram",
    rng: random.Random | None = None,
) -> dict[Any, int]:
    """Release a count histogram under ``epsilon``-DP (Laplace mechanism).

    Adds independent ``Laplace(sensitivity/epsilon)`` noise to every bin. When
    ``non_negative`` (default), negative noisy counts are clamped to 0 and all
    counts rounded to integers; clamping/rounding is post-processing and does not
    weaken the DP guarantee (Dwork & Roth 2014, Prop 2.1). If an ``accountant``
    is given the ``epsilon`` is charged first, fail-closed.
    """
    if accountant is not None:
        accountant.spend(epsilon, 0.0, label=label)
    items = counts.items() if isinstance(counts, Mapping) else counts
    scale = laplace_scale(epsilon, histogram_l1_sensitivity(unbounded=unbounded))
    out: dict[Any, int] = {}
    for key, value in items:
        noisy = value + laplace_noise(scale, rng)
        if non_negative:
            noisy = max(0.0, noisy)
        out[key] = int(round(noisy))
    return out


def dp_count(
    value: int,
    epsilon: float,
    *,
    unbounded: bool = True,
    non_negative: bool = True,
    accountant: PrivacyAccountant | None = None,
    label: str = "count",
    rng: random.Random | None = None,
) -> int:
    """Release a single count under ``epsilon``-DP (a one-bin histogram)."""
    result = dp_histogram(
        {"_": value},
        epsilon,
        unbounded=unbounded,
        non_negative=non_negative,
        accountant=accountant,
        label=label,
        rng=rng,
    )
    return result["_"]
