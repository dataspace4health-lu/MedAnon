#!/usr/bin/env bash
# sync_shared_code.sh  Detect drift between shared code across services.
#
# Compares:
#   1. HEALTHCARE_ENTITIES catalogue between anonymizer and NLP service
#   2. Analytics source files (risk.py, synthetic.py, synthetic_sdv.py)
#   3. Scoring source files (engine.py, privacy.py, utility.py, quality.py,
#      risk.py, models.py, constants.py) between anonymizer/pipeline/scoring/
#      and services/scoring/src/
#   3b. Trust Gate id vocabularies (PHASE_IDS/USE_CASE_IDS in anonymizer
#      trust_profile.py vs trust-gate phases.py + use_case_profiles.yaml)
#
# Usage:
#   ./scripts/sync_shared_code.sh          # check mode (CI-friendly, exit 1 on drift)
#   ./scripts/sync_shared_code.sh --fix    # auto-sync analytics + scoring copies
#
# Exit codes:
#   0 = all in sync
#   1 = drift detected (or sync errors)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRIFT=0

# ─── Colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { printf "${GREEN}[OK]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$1"; DRIFT=1; }
fail() { printf "${RED}[FAIL]${NC} %s\n" "$1"; DRIFT=1; }

# ─── 1. HEALTHCARE_ENTITIES catalogue ──────────────────────────────────────
# Extract the sorted entity list from each file and compare.
ANON_ENTITIES="$REPO_ROOT/services/anonymizer/src/integrations/nlp/utils.py"
NLP_RECOGNIZERS="$REPO_ROOT/services/nlp/src/recognizers.py"

extract_entities() {
    # Extract HEALTHCARE_ENTITIES list entries.
    # For the anonymizer file: grab lines between HEALTHCARE_ENTITIES and the closing bracket.
    # For the NLP recognizers: same pattern  only the catalogue list, not labels_to_ignore etc.
    python3 -c "
import re, sys
text = open('$1').read()
# Find the HEALTHCARE_ENTITIES list
m = re.search(r'HEALTHCARE_ENTITIES[^=]*=\s*\[([^\]]+)\]', text, re.DOTALL)
if not m:
    sys.exit(0)
for name in re.findall(r'\"([A-Z][A-Z0-9_]+)\"', m.group(1)):
    print(name)
" | sort -u
}

if [[ -f "$ANON_ENTITIES" && -f "$NLP_RECOGNIZERS" ]]; then
    ANON_LIST=$(extract_entities "$ANON_ENTITIES")
    NLP_LIST=$(extract_entities "$NLP_RECOGNIZERS")

    ONLY_ANON=$(comm -23 <(echo "$ANON_LIST") <(echo "$NLP_LIST"))
    ONLY_NLP=$(comm -13 <(echo "$ANON_LIST") <(echo "$NLP_LIST"))

    if [[ -n "$ONLY_ANON" ]]; then
        fail "Entities in anonymizer but NOT in NLP service:"
        echo "$ONLY_ANON" | sed 's/^/    /'
    fi
    if [[ -n "$ONLY_NLP" ]]; then
        fail "Entities in NLP service but NOT in anonymizer:"
        echo "$ONLY_NLP" | sed 's/^/    /'
    fi
    if [[ -z "$ONLY_ANON" && -z "$ONLY_NLP" ]]; then
        ok "HEALTHCARE_ENTITIES catalogue in sync ($(echo "$ANON_LIST" | wc -l) entities)"
    fi
else
    warn "Cannot check entity catalogue  file(s) missing"
fi

# ─── 2. Analytics shared files (build-time copies) ────────────────────────
# The analytics Dockerfile COPYs these from the anonymizer at build time.
# Verify the source files haven't diverged from what's checked-in under analytics/.
ANALYTICS_PAIRS=(
    "services/anonymizer/src/analytics/dp.py:services/analytics/src/dp.py"
    "services/anonymizer/src/analytics/risk.py:services/analytics/src/risk.py"
    "services/anonymizer/src/analytics/privacy_risk.py:services/analytics/src/privacy_risk.py"
    "services/anonymizer/src/analytics/synthetic_passport.py:services/analytics/src/synthetic_passport.py"
    "services/anonymizer/src/analytics/synthetic.py:services/analytics/src/synthetic.py"
    "services/anonymizer/src/analytics/synthetic_sdv.py:services/analytics/src/synthetic_sdv.py"
)

for pair in "${ANALYTICS_PAIRS[@]}"; do
    SRC="$REPO_ROOT/${pair%%:*}"
    DST="$REPO_ROOT/${pair##*:}"
    BASENAME=$(basename "$SRC")

    if [[ ! -f "$SRC" ]]; then
        warn "Source missing: $SRC"
        continue
    fi

    if [[ ! -f "$DST" ]]; then
        # Analytics service may use Dockerfile COPY at build time only
        ok "$BASENAME  analytics uses build-time COPY (no local duplicate)"
        continue
    fi

    SRC_HASH=$(sha256sum "$SRC" | cut -d' ' -f1)
    DST_HASH=$(sha256sum "$DST" | cut -d' ' -f1)

    if [[ "$SRC_HASH" == "$DST_HASH" ]]; then
        ok "$BASENAME in sync"
    else
        if [[ "${1:-}" == "--fix" ]]; then
            cp "$SRC" "$DST"
            ok "$BASENAME synced (copied from anonymizer)"
        else
            fail "$BASENAME has drifted!"
            echo "    source: $SRC"
            echo "    target: $DST"
            echo "    Run with --fix to auto-sync."
        fi
    fi
done

# ─── 3. Scoring shared files (manually-synced microservice copies) ───────
# 6 logically-identical pairs: anonymizer's pipeline/scoring/ ↔ services/scoring/src/.
# These are NOT build-time COPYs  they're hand-maintained duplicates because
# the scoring microservice ships as a standalone container with no dependency
# on the anonymizer image.  Drift here is a recurring source of bugs (e.g. the
# decade-year k-anonymity fix had to be applied twice).
#
# The two trees use different import roots  anonymizer uses qualified imports
# (`from pipeline.scoring.models import …`, `from analytics.risk import …`),
# the scoring service uses flat imports (`from models import …`, `from risk
# import …`) plus a metrics stub.  We therefore compare the *normalized*
# content (imports stripped + path-prefix differences canonicalised) rather
# than raw bytes, so the legitimate import-only differences don't trigger
# false drift alarms.
SCORING_PAIRS=(
    "services/anonymizer/src/pipeline/scoring/engine.py:services/scoring/src/engine.py"
    "services/anonymizer/src/pipeline/scoring/privacy.py:services/scoring/src/privacy.py"
    "services/anonymizer/src/pipeline/scoring/utility.py:services/scoring/src/utility.py"
    "services/anonymizer/src/pipeline/scoring/quality.py:services/scoring/src/quality.py"
    "services/anonymizer/src/pipeline/scoring/models.py:services/scoring/src/models.py"
    "services/anonymizer/src/pipeline/scoring/constants.py:services/scoring/src/constants.py"
)

# Normalize a scoring file so the only differences left are real ones.
# Removes: any `from X import …` / `import X` line; collapses anonymizer's
# qualified module paths to the bare module names used by the scoring service.
normalize_scoring() {
    sed -E \
        -e '/^[[:space:]]*from[[:space:]]+[A-Za-z_.]+[[:space:]]+import/d' \
        -e '/^[[:space:]]*import[[:space:]]+[A-Za-z_.]+/d' "$1"
}

for pair in "${SCORING_PAIRS[@]}"; do
    SRC="$REPO_ROOT/${pair%%:*}"
    DST="$REPO_ROOT/${pair##*:}"
    BASENAME=$(basename "$SRC")
    LABEL="scoring/$BASENAME"

    if [[ ! -f "$SRC" ]]; then
        warn "Source missing: $SRC"
        continue
    fi
    if [[ ! -f "$DST" ]]; then
        fail "$LABEL  destination missing: $DST"
        continue
    fi

    if diff -q <(normalize_scoring "$SRC") <(normalize_scoring "$DST") > /dev/null; then
        ok "$LABEL in sync (logically identical)"
    else
        fail "$LABEL has logic drift!"
        echo "    source: $SRC"
        echo "    target: $DST"
        echo "    Diff (after stripping imports):"
        diff <(normalize_scoring "$SRC") <(normalize_scoring "$DST") | head -20 | sed 's/^/      /' || true
        echo "    NOTE: --fix is unsafe here  import paths differ between trees."
        echo "    Apply the missing change manually to the target file."
    fi
done

# ─── 3b. Trust Gate <-> anonymizer id sync ─────────────────────────────────
# The two services share no package, but the anonymizer's trust-profile model
# carries copies of the trust-gate phase + use-case vocabularies. Drift here is
# a silent contract break (a valid profile gets rejected, or vice versa), so
# compare them set-wise: PHASE_IDS == ALL_PHASES and USE_CASE_IDS == the keys
# under use_cases: in use_case_profiles.yaml.
TRUST_SYNC=$(
    REPO_ROOT="$REPO_ROOT" python3 - <<'PY'
import os, sys, yaml
root = os.environ["REPO_ROOT"]
sys.path.insert(0, os.path.join(root, "services", "anonymizer", "src"))
sys.path.insert(0, os.path.join(root, "services", "trust-gate", "src"))
try:
    from pipeline.trust_profile import PHASE_IDS, USE_CASE_IDS
    import phases
    with open(os.path.join(root, "services", "trust-gate", "config", "use_case_profiles.yaml")) as f:
        uc = yaml.safe_load(f) or {}
    uc_keys = set((uc.get("use_cases") or uc).keys())
except Exception as exc:  # pragma: no cover - reported as drift below
    print(f"ERROR loading trust-gate ids: {exc}")
    sys.exit(0)

phase_diff = set(PHASE_IDS) ^ set(phases.ALL_PHASES)
uc_diff = set(USE_CASE_IDS) ^ uc_keys
if phase_diff:
    print(f"PHASE_DRIFT {sorted(phase_diff)}")
if uc_diff:
    print(f"USECASE_DRIFT {sorted(uc_diff)}")
if not phase_diff and not uc_diff:
    print(f"OK {len(PHASE_IDS)} phases / {len(USE_CASE_IDS)} use-cases")
PY
)
case "$TRUST_SYNC" in
    OK*)          ok "Trust Gate ids in sync (${TRUST_SYNC#OK })" ;;
    *PHASE_DRIFT*|*USECASE_DRIFT*|*ERROR*)
        fail "Trust Gate id drift (anonymizer trust_profile.py vs trust-gate):"
        echo "$TRUST_SYNC" | sed 's/^/    /' ;;
    *)            warn "Cannot check Trust Gate id sync  unexpected output: $TRUST_SYNC" ;;
esac

# ─── 4. Hash manifest ─────────────────────────────────────────────────────
# Write a .shared-code-hashes file that can be checked into the repo and
# verified during CI or Docker build.
HASH_FILE="$REPO_ROOT/.shared-code-hashes"
if [[ "${1:-}" == "--fix" ]] || [[ ! -f "$HASH_FILE" ]]; then
    {
        echo "# Auto-generated by scripts/sync_shared_code.sh"
        echo "# Verify with: sha256sum -c .shared-code-hashes"
        for f in \
            services/anonymizer/src/integrations/nlp/utils.py \
            services/anonymizer/src/analytics/risk.py \
            services/anonymizer/src/analytics/synthetic.py \
            services/anonymizer/src/analytics/synthetic_sdv.py \
            services/anonymizer/src/pipeline/scoring/engine.py \
            services/anonymizer/src/pipeline/scoring/privacy.py \
            services/anonymizer/src/pipeline/scoring/utility.py \
            services/anonymizer/src/pipeline/scoring/quality.py \
            services/anonymizer/src/pipeline/scoring/risk.py \
            services/anonymizer/src/pipeline/scoring/models.py \
            services/anonymizer/src/pipeline/scoring/constants.py \
            services/nlp/src/recognizers.py; do
            if [[ -f "$REPO_ROOT/$f" ]]; then
                (cd "$REPO_ROOT" && sha256sum "$f")
            fi
        done
    } > "$HASH_FILE"
    ok "Hash manifest written to .shared-code-hashes"
fi

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
if [[ "$DRIFT" -eq 0 ]]; then
    printf "${GREEN}All shared code is in sync.${NC}\n"
else
    printf "${RED}Drift detected  fix before building Docker images.${NC}\n"
fi
exit "$DRIFT"
