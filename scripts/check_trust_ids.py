#!/usr/bin/env python3
"""Fail if the Trust Gate id vocabularies drift between the two services.

The anonymizer's trust-profile model (`pipeline/trust_profile.py`) carries copies
of the trust-gate phase + use-case vocabularies; the services share no package,
so drift is a silent contract break (a valid profile gets rejected, or a profile
references a phase the gate no longer knows). This is a narrow, CI-friendly gate
that checks only those two vocabularies, independent of the broader, manually
synced scoring comparison in ``sync_shared_code.sh``.

Exit 0 = in sync; exit 1 = drift (or load error).
"""

from __future__ import annotations

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services", "anonymizer", "src"))
sys.path.insert(0, os.path.join(ROOT, "services", "trust-gate", "src"))


def main() -> int:
    try:
        from domain.trust import PHASE_IDS, USE_CASE_IDS  # type: ignore
        import phases  # type: ignore

        profiles_path = os.path.join(
            ROOT, "services", "trust-gate", "config", "use_case_profiles.yaml"
        )
        with open(profiles_path) as fh:
            doc = yaml.safe_load(fh) or {}
        uc_keys = set((doc.get("use_cases") or doc).keys())
    except Exception as exc:  # noqa: BLE001 - any load failure is reportable drift
        print(f"[FAIL] cannot load Trust Gate ids: {exc}")
        return 1

    phase_diff = set(PHASE_IDS) ^ set(phases.ALL_PHASES)
    uc_diff = set(USE_CASE_IDS) ^ uc_keys

    if phase_diff:
        print(
            "[FAIL] PHASE_IDS (anonymizer) != ALL_PHASES (trust-gate); "
            f"symmetric difference: {sorted(phase_diff)}"
        )
    if uc_diff:
        print(
            "[FAIL] USE_CASE_IDS (anonymizer) != use_case_profiles.yaml keys; "
            f"symmetric difference: {sorted(uc_diff)}"
        )
    if phase_diff or uc_diff:
        print(
            "Update services/anonymizer/src/pipeline/trust_profile.py and "
            "services/trust-gate together (see the KEEP IN SYNC comments)."
        )
        return 1

    print(f"[OK] Trust Gate ids in sync ({len(PHASE_IDS)} phases / {len(USE_CASE_IDS)} use-cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
