#!/usr/bin/env bash
# sync_shared_code.sh - Detect drift in the one remaining cross-service copy.
#
# The scoring engine, analytics, domain contracts and Trust Gate vocabulary are
# now a single installable package (packages/medanon-core); their old copies and
# the sync/parity checks for them are retired (the package IS the source of
# truth, and trust-gate asserts the vocab at import + in test_trust_vocab_parity).
#
# What remains here: the NLP HEALTHCARE_ENTITIES catalogue, which is still
# duplicated between the anonymizer's NLP integration and the NLP microservice
# (a separate de-duplication, out of scope for the medanon-core extraction).
#
# Usage:  ./scripts/sync_shared_code.sh    # exit 1 on drift (CI-friendly)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRIFT=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { printf "${GREEN}[OK]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$1"; DRIFT=1; }
fail() { printf "${RED}[FAIL]${NC} %s\n" "$1"; DRIFT=1; }

# ─── NLP HEALTHCARE_ENTITIES catalogue ──────────────────────────────────────
ANON_ENTITIES="$REPO_ROOT/services/anonymizer/src/integrations/nlp/utils.py"
NLP_RECOGNIZERS="$REPO_ROOT/services/nlp/src/recognizers.py"

extract_entities() {
    python3 -c "
import re, sys
text = open('$1').read()
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
    warn "Cannot check entity catalogue - file(s) missing"
fi

if [[ "$DRIFT" -ne 0 ]]; then
    echo ""
    echo -e "${RED}NLP entity catalogue drift detected.${NC}"
    exit 1
fi
echo ""
echo -e "${GREEN}NLP entity catalogue in sync.${NC}"
