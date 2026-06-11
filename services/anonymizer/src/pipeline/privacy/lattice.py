"""OLA-style generalization lattice solver.

Searches the generalization lattice for the *minimal*-information-loss
combination of per-QI levels that satisfies a target k-anonymity (and
optionally l-diversity) guarantee, bounded by a suppression cap.

Algorithm
---------
Bottom-up monotone lattice search (Optimal Lattice Anonymisation / OLA):

1. Each lattice *node* is a tuple of per-QI level indices
   (e.g. (0, 2, 1) = date at level 0, zip at level 2, age at level 1).
2. Monotonicity: applying a higher level never *decreases* k, so:
   - If a node is *infeasible* (suppression > cap or k < target even after
     suppression), ALL nodes with lower-or-equal levels are also infeasible.
   - If a node is *feasible* AND has minimum information loss, all nodes with
     higher-or-equal levels are also feasible (but wasteful).
3. The search explores nodes in ascending information-loss order, prunes
   dominated infeasible regions, and returns the first feasible node found.

Complexity
----------
Product of (max_level + 1) over QIs.  Typical: 3 QIs × ≤ 6 levels → ≤ 216
nodes.  Capped at ``MAX_QI_COUNT = 5``, worst case 6^5 = 7776 nodes, still
fast.  The solver logs the node count and search time.

Information-loss metric
-----------------------
``IL(node) = Σ (level_i / max_level_i) / n_qi``  (normalised mean level)
Range [0, 1] where 0 = no generalisation and 1 = full suppression on all QIs.

Public API
----------
    plan = solve(qi_index, privacy_model)
    plan.levels           # dict[qi_path → int level]
    plan.suppressed_ids   # set[str]  patient ids to suppress
    plan.achieved_k       # int
    plan.achieved_l       # int | None
    plan.information_loss # float
    plan.feasible         # bool (False when on_unsatisfiable='max_generalize')
    plan.node             # tuple[int, ...] chosen level vector
"""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.privacy.qi_index import QiIndex

_log = logging.getLogger("medanon.privacy.lattice")

# Hard cap: refuse configs with more QIs than this (lattice would be too large).
MAX_QI_COUNT = 5


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass
class GeneralizationPlan:
    """Result of the lattice solver.

    Attributes
    ----------
    levels            Chosen level per QI (key = FHIRPath string).
    node              Level vector as a tuple (order = qi_index.qi_paths).
    suppressed_ids    Patient ids to suppress (post-generalisation small classes).
    achieved_k        Minimum equivalence-class size after applying the plan.
    achieved_l        Minimum l-diversity value (None if not computed).
    suppressed_count  Number of Patients suppressed.
    suppression_rate  suppressed_count / total_patients.
    information_loss  Normalised mean level (0 = no generalisation).
    feasible          True if the plan meets the target guarantee within cap.
    on_unsatisfiable  Action taken when no feasible node found ('max_generalize'
                      or 'fail'); 'fail' causes the executor to abort the job.
    total_nodes_evaluated  How many lattice nodes were checked.
    solve_time_sec    Wall time for the solver.
    """

    levels: dict[str, int] = field(default_factory=dict)
    node: tuple[int, ...] = field(default_factory=tuple)
    suppressed_ids: set[str] = field(default_factory=set)
    achieved_k: int = 0
    achieved_l: int | None = None
    suppressed_count: int = 0
    suppression_rate: float = 0.0
    information_loss: float = 0.0
    feasible: bool = False
    on_unsatisfiable: str = "max_generalize"
    total_nodes_evaluated: int = 0
    solve_time_sec: float = 0.0


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _apply_levels(
    qi_tuples: list[tuple[str, ...]],
    node: tuple[int, ...],
    hierarchies: list,
) -> list[tuple[str, ...]]:
    """Apply a level vector to a list of raw QI tuples.

    Returns a new list of generalised QI tuples (same length).
    """
    result = []
    for raw in qi_tuples:
        gen = tuple(h.apply(lvl, val) for h, lvl, val in zip(hierarchies, node, raw))
        result.append(gen)
    return result


def _compute_suppression(
    generalised: list[tuple[str, ...]],
    patient_ids: list[str],
    target_k: int,
) -> tuple[int, set[str]]:
    """Count records in classes smaller than target_k and collect their ids.

    Returns ``(min_class_size_after_suppression, suppressed_patient_id_set)``.
    """
    from collections import Counter

    counts = Counter(generalised)
    suppressed: set[str] = set()
    for qi_tuple, pid in zip(generalised, patient_ids):
        if counts[qi_tuple] < target_k:
            suppressed.add(pid)
    # After removing suppressed patients, recompute surviving class sizes.
    surviving = [
        qt for qt, pid in zip(generalised, patient_ids) if pid not in suppressed
    ]
    if not surviving:
        return 0, suppressed
    surviving_counts = Counter(surviving)
    min_k = min(surviving_counts.values())
    return min_k, suppressed


def _compute_l_diversity(
    generalised: list[tuple[str, ...]],
    patient_ids: list[str],
    suppressed_ids: set[str],
    conditions_by_patient: dict[str, set[str]],
) -> int:
    """Compute min l-diversity over surviving equivalence classes.

    Returns 0 when conditions data is absent or no surviving patients exist.
    """
    from collections import defaultdict

    if not conditions_by_patient:
        return 0
    group_codes: dict[tuple, set[str]] = defaultdict(set)
    for qi_tuple, pid in zip(generalised, patient_ids):
        if pid in suppressed_ids:
            continue
        codes = conditions_by_patient.get(pid) or set()
        group_codes[qi_tuple].update(codes)
    if not group_codes:
        return 0
    return min(len(codes) for codes in group_codes.values())


def _information_loss(node: tuple[int, ...], max_levels: list[int]) -> float:
    """Normalised mean level (0 = no generalisation, 1 = full suppression)."""
    if not node:
        return 0.0
    return sum(lvl / ml for lvl, ml in zip(node, max_levels)) / len(node)


# ---------------------------------------------------------------------------
# Solver entry point
# ---------------------------------------------------------------------------


def solve(qi_index: "QiIndex", privacy_model: dict) -> GeneralizationPlan:
    """Search the generalization lattice and return the optimal plan.

    Raises
    ------
    ValueError
        When ``on_unsatisfiable='fail'`` and no feasible node exists.
    """
    from pipeline.privacy.hierarchies import get_hierarchy

    t0 = time.monotonic()
    target_k: int = privacy_model["target_k"]
    target_l: int | None = privacy_model.get("target_l")
    max_suppression: float = privacy_model.get("max_suppression", 0.05)
    on_unsatisfiable: str = privacy_model.get("on_unsatisfiable", "max_generalize")

    qi_paths = qi_index.qi_paths
    qi_kinds = qi_index.qi_kinds
    n_qi = len(qi_paths)

    if n_qi == 0:
        raise ValueError("privacy_model.quasi_identifiers is empty — nothing to solve")
    if n_qi > MAX_QI_COUNT:
        raise ValueError(
            f"privacy_model has {n_qi} quasi-identifiers; maximum is {MAX_QI_COUNT}"
        )

    hierarchies = [get_hierarchy(k) for k in qi_kinds]
    max_levels = [h.max_level() for h in hierarchies]
    total_patients = len(qi_index.patient_ids)

    if total_patients == 0:
        _log.warning(
            "solve: no Patient records in index — returning trivially-feasible plan"
        )
        return GeneralizationPlan(
            levels={p: 0 for p in qi_paths},
            node=tuple([0] * n_qi),
            suppressed_ids=set(),
            achieved_k=0,
            feasible=True,
            on_unsatisfiable=on_unsatisfiable,
        )

    # Enumerate all lattice nodes in ascending information-loss order.
    # (This is an exhaustive search — tractable because the lattice is small.)
    level_ranges = [range(ml + 1) for ml in max_levels]
    all_nodes = list(itertools.product(*level_ranges))
    all_nodes.sort(key=lambda nd: _information_loss(nd, max_levels))

    total_nodes = len(all_nodes)
    _log.info(
        "solve: target_k=%d target_l=%s max_suppression=%.2f "
        "n_qi=%d total_nodes=%d total_patients=%d",
        target_k,
        target_l,
        max_suppression,
        n_qi,
        total_nodes,
        total_patients,
    )

    best_plan: GeneralizationPlan | None = None
    nodes_evaluated = 0

    for node in all_nodes:
        nodes_evaluated += 1
        generalised = _apply_levels(qi_index.patient_qi_tuples, node, hierarchies)
        min_k_after_suppress, suppressed_ids = _compute_suppression(
            generalised, qi_index.patient_ids, target_k
        )
        supp_rate = len(suppressed_ids) / total_patients
        il = _information_loss(node, max_levels)

        # Feasibility check 1: suppression within cap
        if supp_rate > max_suppression:
            continue

        # Feasibility check 2: achieving target_k among survivors
        if min_k_after_suppress < target_k and len(suppressed_ids) < total_patients:
            continue

        # Feasibility check 3: l-diversity (optional)
        achieved_l: int | None = None
        if target_l is not None and qi_index.conditions_by_patient:
            achieved_l = _compute_l_diversity(
                generalised,
                qi_index.patient_ids,
                suppressed_ids,
                qi_index.conditions_by_patient,
            )
            if achieved_l < target_l:
                continue

        # This node is feasible.  Since nodes are sorted by information_loss
        # (ascending), the first feasible node is also the optimal one.
        best_plan = GeneralizationPlan(
            levels={p: lvl for p, lvl in zip(qi_paths, node)},
            node=node,
            suppressed_ids=suppressed_ids,
            achieved_k=min_k_after_suppress,
            achieved_l=achieved_l,
            suppressed_count=len(suppressed_ids),
            suppression_rate=supp_rate,
            information_loss=il,
            feasible=True,
            on_unsatisfiable=on_unsatisfiable,
            total_nodes_evaluated=nodes_evaluated,
            solve_time_sec=time.monotonic() - t0,
        )
        break

    if best_plan is None:
        # No feasible node found.
        elapsed = time.monotonic() - t0
        _log.warning(
            "solve: no feasible node (target_k=%d target_l=%s max_suppression=%.2f). "
            "on_unsatisfiable=%s nodes_evaluated=%d",
            target_k,
            target_l,
            max_suppression,
            on_unsatisfiable,
            nodes_evaluated,
        )

        if on_unsatisfiable == "fail":
            raise ValueError(
                f"No generalization satisfies k={target_k} within "
                f"max_suppression={max_suppression:.0%}. "
                f"Increase max_suppression or lower target_k, "
                f"or set on_unsatisfiable='max_generalize'."
            )

        # max_generalize: use the top node (full suppression of all QIs)
        # and suppress whatever remains unsatisfied.
        top_node = tuple(max_levels)
        generalised = _apply_levels(qi_index.patient_qi_tuples, top_node, hierarchies)
        min_k, suppressed_ids = _compute_suppression(
            generalised, qi_index.patient_ids, target_k
        )
        # Force-suppress remaining small classes even past the cap (we've
        # already tried everything; document this in the plan).
        achieved_l = None
        if target_l is not None and qi_index.conditions_by_patient:
            achieved_l = _compute_l_diversity(
                generalised,
                qi_index.patient_ids,
                suppressed_ids,
                qi_index.conditions_by_patient,
            )
        best_plan = GeneralizationPlan(
            levels={p: lvl for p, lvl in zip(qi_paths, top_node)},
            node=top_node,
            suppressed_ids=suppressed_ids,
            achieved_k=min_k,
            achieved_l=achieved_l,
            suppressed_count=len(suppressed_ids),
            suppression_rate=len(suppressed_ids) / max(total_patients, 1),
            information_loss=_information_loss(top_node, max_levels),
            feasible=False,
            on_unsatisfiable=on_unsatisfiable,
            total_nodes_evaluated=nodes_evaluated,
            solve_time_sec=elapsed,
        )

    _log.info(
        "solve_done feasible=%s achieved_k=%d achieved_l=%s "
        "suppressed=%d/%.1f%% il=%.3f node=%s nodes_evaluated=%d time=%.2fs",
        best_plan.feasible,
        best_plan.achieved_k,
        best_plan.achieved_l,
        best_plan.suppressed_count,
        best_plan.suppression_rate * 100,
        best_plan.information_loss,
        best_plan.node,
        best_plan.total_nodes_evaluated,
        best_plan.solve_time_sec,
    )
    return best_plan
