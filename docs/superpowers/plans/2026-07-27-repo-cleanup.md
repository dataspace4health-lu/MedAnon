# Repo Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repository accurate, non-duplicated and self-verifying, so a new engineer can trust every document they read.

**Architecture:** Seven gated phases. Land the in-flight refactor first, remove uncurated files, collapse two documentation systems into one canonical home, then build a mechanical drift checker whose failure list drives the content rewrite. Environment and agent-context cleanup follow last.

**Tech Stack:** Python 3.12/3.13 (stdlib only for scripts), pytest, ruff, import-linter, Docker Compose, Docusaurus 3.6.3, GNU Make.

## Global Constraints

- Every claim written into a document must be traceable to a command that verifies it. No number is copied forward from an older document without re-deriving it.
- No em dashes in any documentation. Use hyphens or restructure the sentence.
- No emojis anywhere: code, docs, commits, CLI output.
- Commit messages carry no AI attribution and no `Co-Authored-By` trailer.
- Never commit or push without explicit user authorisation at that gate.
- Scope commits per module. One concern per commit.
- Secret values from `.env` are never copied into documentation, commit messages, or terminal output that gets pasted into a doc.
- `.env.example` is CI-clean and shrinks for no reason. It may only gain keys.
- `**/tests/` stays gitignored. This is a decided policy, not a defect to fix.
- The AUTH.2 finding (nginx performs no authentication, `/fhir/*` serves identified data including an SSN identifier) stays documented. It is an accepted open risk.
- Verification commands run from the repository root unless the step says otherwise.

## Reference: verified baseline (2026-07-27)

| Fact | Value |
|---|---|
| Compose services | 28 total, 15 always-on, 13 opt-in across 7 profiles |
| Compose networks / volumes | 3 / 14 |
| Config profiles on disk | 4 (`config.yaml`, `config_gdpr_eu.yaml`, `config_hipaa_safe_harbor.yaml`, `config_value_masking.yaml`) |
| `docs/` top level | 17 files, 8627 lines |
| `docs/trust-gate/` | 7 files, 2785 lines |
| `website/docs/` | 38 files, 8104 lines |
| `.env` | 208 active keys, 7 dead, 11 posture flags unset |
| `.env.example` | 63 keys, `check_env.py` clean |
| Client pages | 28 |
| Claude memory files | 56 |

---

## Phase 0: Land the Refactor branch

### Task 0.1: Verify the working tree is green before committing anything

**Files:**
- Modify: none

- [ ] **Step 1: Confirm the branch and change count**

Run:
```bash
cd /home/developer/privacy-toolkit
git branch --show-current
git status --porcelain | wc -l
```
Expected: `Refactor`, and a count near 126.

- [ ] **Step 2: Run the full pre-push gate**

Run:
```bash
make ci-local
```
Expected: ends with `ci-local: all green`.

If it fails, stop. Do not proceed to any commit. Fix the underlying code; never weaken a test to make it pass. Report the failure output verbatim.

- [ ] **Step 3: Record the baseline for later comparison**

Run:
```bash
mkdir -p /tmp/claude-1001/-home-developer-privacy-toolkit/89cf14c7-39d4-42d9-bec3-09b1b131a1a3/scratchpad
git status --porcelain > /tmp/claude-1001/-home-developer-privacy-toolkit/89cf14c7-39d4-42d9-bec3-09b1b131a1a3/scratchpad/phase0-baseline.txt
wc -l < /tmp/claude-1001/-home-developer-privacy-toolkit/89cf14c7-39d4-42d9-bec3-09b1b131a1a3/scratchpad/phase0-baseline.txt
```

### Task 0.2: Commit the medanon-core extraction

**Files:**
- Commit: `packages/medanon-core/src/analytics/dp.py`, `packages/medanon-core/src/analytics/risk.py`, `packages/medanon-core/src/domain/scoring.py`, `packages/medanon-core/src/domain/workflows.py`, `packages/medanon-core/src/scoring/engine.py`, `packages/medanon-core/src/scoring/quality.py`

**Interfaces:**
- Produces: a clean `packages/` tree that Tasks 0.3 and 0.4 build on.

- [ ] **Step 1: Review exactly what changed**

Run:
```bash
git diff --stat packages/medanon-core
```
Expected: 6 modified files.

- [ ] **Step 2: Ask the user to authorise commits for this phase**

Present the 6-file list and the proposed message. Wait for explicit approval. Do not proceed without it.

- [ ] **Step 3: Commit**

```bash
git add packages/medanon-core
git commit -m "refactor(core): finalise shared analytics/domain/scoring surface"
```

- [ ] **Step 4: Verify the commit is scoped**

Run:
```bash
git show --stat HEAD | tail -12
```
Expected: only `packages/medanon-core` paths.

### Task 0.3: Commit the anonymizer changes

**Files:**
- Commit: 57 paths under `services/anonymizer/`, including deletions of `config/config_gpas.yaml`, `config/config_k_anonymity.yaml`, `config/config_research_pseudonymous.yaml`, `config/config_structure_preserving.yaml`, `src/formats/cda.py`, `src/pipeline/subscriptions/dispatcher.py`

- [ ] **Step 1: Separate the profile deletions from the code changes**

Run:
```bash
git add services/anonymizer/config
git commit -m "refactor(anonymizer): drop four bundled profiles, keep the four supported ones"
```

- [ ] **Step 2: Verify the remaining profiles**

Run:
```bash
ls -1 services/anonymizer/config/*.yaml | wc -l
```
Expected: `4`

- [ ] **Step 3: Commit the source changes**

```bash
git add services/anonymizer/src
git commit -m "refactor(anonymizer): resolve shared code against medanon-core, drop legacy cda scrubber and subscription dispatcher"
```

- [ ] **Step 4: Verify nothing under services/anonymizer remains uncommitted**

Run:
```bash
git status --porcelain services/anonymizer
```
Expected: empty output.

### Task 0.4: Commit the infrastructure, client and docs moves

**Files:**
- Commit: `services/gpas/` (6), `services/nlp/` (2), `services/fhir-server/`, `services/fhir-target/`, `monitoring/` (2), `docker-compose.yml`, `Makefile`, `.gitignore`, `.env.example`, `client/` (8), `docs/` trust-gate move (7 deletions + 7 additions) and 9 modified docs, `README.md`, 8 `website/docs/` files

- [ ] **Step 1: Commit the service and infrastructure changes**

```bash
git add services/gpas services/nlp services/fhir-server services/fhir-target monitoring docker-compose.yml Makefile .gitignore .env.example
git commit -m "chore(infra): align compose, makefile and service configs with the core split"
```

- [ ] **Step 2: Commit the client changes**

```bash
git add client
git commit -m "refactor(client): simplify shared panels and classification API surface"
```

- [ ] **Step 3: Commit the documentation moves**

```bash
git add docs README.md website/docs
git commit -m "docs: move trust-gate guides into docs/trust-gate/ and refresh affected pages"
```

- [ ] **Step 4: Verify the tree**

Run:
```bash
git status --porcelain | grep -v '^??' | wc -l
```
Expected: `0`. Only untracked entries remain, which Phase 1 triages.

### Task 0.5: Phase 0 gate

**Files:**
- Modify: none. This task only verifies.

- [ ] **Step 1: Re-run the full gate on the committed tree**

Run:
```bash
make ci-local
```
Expected: `ci-local: all green`.

- [ ] **Step 2: Confirm commit scoping**

Run:
```bash
git log --oneline -6
```
Expected: 6 commits, each touching one concern.

---

## Phase 1: Hygiene

### Task 1.1: Remove uncurated local artifacts

**Files:**
- Delete: `.env.bak.1783666157`, `scripts/__pycache__/`

- [ ] **Step 1: Confirm the backup differs only trivially and holds no unique key**

Run:
```bash
diff <(grep -oE '^[A-Z_0-9]+=' .env | sort) <(grep -oE '^[A-Z_0-9]+=' .env.bak.1783666157 | sort)
```
Expected: no key present only in the backup. If a key IS unique to the backup, stop and report it before deleting.

- [ ] **Step 2: Delete**

```bash
rm -f .env.bak.1783666157
rm -rf scripts/__pycache__
```

- [ ] **Step 3: Verify**

Run:
```bash
ls -la .env* && ls -d scripts/__pycache__ 2>&1 | tail -1
```
Expected: only `.env` and `.env.example`; `scripts/__pycache__` reported missing.

### Task 1.2: Triage every untracked path

**Files:**
- Modify: `.gitignore` (only if a path is ignored)

- [ ] **Step 1: List what needs a verdict**

Run:
```bash
git status --porcelain | grep '^??'
```
Expected: `bench/`, `docker-compose.bench.yml`, `docker-compose.override.yml`, `docs/overview.md`, `docs/superpowers/`, `monitoring/rules/`, `output`, `services/gpas/config/`, `services/gpas/sqls_pg/03_init_gpas_domain.sql`

- [ ] **Step 2: Apply these verdicts**

| Path | Verdict | Reason |
|---|---|---|
| `bench/` | gitignore | Timestamped result JSON, regenerable by `make bench-*` |
| `docker-compose.bench.yml` | track | Referenced by `make bench-services`, breaks on a fresh clone otherwise |
| `docker-compose.override.yml` | gitignore | Local worker overrides, machine-specific |
| `docs/overview.md` | delete | Superseded by `website/docs/intro.mdx`, see Task 2.2 |
| `docs/superpowers/` | track | Spec and plan records for this work |
| `monitoring/rules/` | track | Prometheus alert rules referenced by `monitoring/prometheus.yml` |
| `output` | already ignored | Runtime artifacts, `/output/` is in `.gitignore` |
| `services/gpas/config/` | track | gPAS config files, `.gitignore` says config is tracked |
| `services/gpas/sqls_pg/03_init_gpas_domain.sql` | track | Carries the `psn(pseudonym)` index that fixed the export bottleneck |

- [ ] **Step 3: Verify `monitoring/rules/` is actually referenced before tracking it**

Run:
```bash
grep -n 'rules' monitoring/prometheus.yml
```
Expected: a `rule_files:` entry. If absent, change the verdict to delete and note it.

- [ ] **Step 4: Verify the gPAS index file is what memory claims**

Run:
```bash
grep -in 'pseudonym' services/gpas/sqls_pg/03_init_gpas_domain.sql | head
```
Expected: a `CREATE INDEX` on `psn(pseudonym)`.

- [ ] **Step 5: Apply the ignores**

Add to `.gitignore` under a new section:
```
# ── Benchmark results (regenerable via make bench-*) ──────────────────────────
bench/results/
# ── Local compose overrides (machine-specific) ───────────────────────────────
docker-compose.override.yml
```

- [ ] **Step 6: Track the keepers and delete the superseded**

```bash
rm -f docs/overview.md
git add docker-compose.bench.yml monitoring/rules services/gpas/config services/gpas/sqls_pg/03_init_gpas_domain.sql docs/superpowers .gitignore
```

- [ ] **Step 7: Verify no path lacks a verdict**

Run:
```bash
git status --porcelain | grep '^??' || echo "all triaged"
```
Expected: `all triaged`.

- [ ] **Step 8: Commit**

```bash
git commit -m "chore(repo): triage untracked paths, drop stale env backup"
```

---

## Phase 2: Documentation topology

### Task 2.1: Record the ownership matrix

**Files:**
- Create: `docs/superpowers/specs/2026-07-27-doc-ownership-matrix.md`

**Interfaces:**
- Produces: the deletion list Task 2.2 executes and the merge list Task 2.3 executes.

- [ ] **Step 1: Write the matrix**

Create `docs/superpowers/specs/2026-07-27-doc-ownership-matrix.md` with this content:

```markdown
# Documentation ownership matrix

Canonical home: `website/`. `docs/` keeps only what has no site equivalent.

## Retire from docs/, website page is canonical

| docs/ file | Lines | Canonical website page |
|---|---|---|
| `DEPLOYMENT.md` | 451 | `how-to/deploy-docker.md` |
| `RUNBOOK.md` | 424 | `how-to/operations-runbook.md` |
| `api-reference.md` | 1118 | `reference/api.md` |
| `architecture.md` | 471 | `explanation/architecture.md` |
| `components.md` | 593 | `reference/components/` (5 pages) |
| `connector-integration.md` | 405 | `how-to/integrate-connector.md` |
| `data-flow.md` | 525 | `explanation/data-flow.md` |
| `introduction.md` | 174 | `intro.mdx` |
| `overview.md` | 222 | `intro.mdx` |
| `policies.md` | 181 | `explanation/policies.md` |
| `result-example.md` | 339 | `reference/result-examples.md` |
| `scoring-system.md` | 284 | `explanation/scoring-system.md` |
| `security.md` | 491 | `explanation/security-model.md` |
| `user-manual.md` | 480 | `how-to/use-the-web-ui.md` |
| `TECHNICAL_DOCUMENTATION.md` | 1212 | superseded by the site as a whole |

## Keep in docs/

| Path | Reason |
|---|---|
| `INDEX.md` | Rewritten as a thin contributor index pointing at the site |
| `reference/env-vars.md` | Generated by `check_env.py`, CI-gated, not site content |
| `internal/` | Gitignored review ledgers and roadmaps |
| `superpowers/` | Specs and plans |
| `trust-gate/` (7 files) | The site carries only one Trust Gate how-to page; this deep set has no equivalent |
| `quality-evaluation-methodology.md` | Trust Gate measurement methodology, no site equivalent |

## Rule

No topic may live in both trees. A `docs/` file is deleted only after its
unique content is confirmed present in, or merged into, its canonical page.
```

- [ ] **Step 2: Verify every listed line count**

Run:
```bash
for f in DEPLOYMENT RUNBOOK api-reference architecture components connector-integration data-flow introduction overview policies result-example scoring-system security user-manual TECHNICAL_DOCUMENTATION; do
  [ -f "docs/$f.md" ] && printf "%-32s %s\n" "$f" "$(wc -l < docs/$f.md)"
done
```
Expected: matches the table. `overview.md` is absent if Task 1.2 deleted it; note that in the matrix.

### Task 2.2: Merge unique content before any deletion

**Files:**
- Modify: `website/docs/explanation/security-model.md`, and any other website page found to be missing content

**Interfaces:**
- Consumes: the retire list from Task 2.1.
- Produces: website pages that fully cover each retired `docs/` file.

- [ ] **Step 1: Confirm the AUTH.2 finding survives**

This is the highest-risk item in the whole cleanup. Run:
```bash
grep -n 'AUTH.2\|auth_request\|reachable only via' docs/security.md | head
grep -n 'AUTH.2\|auth_request\|no auth check\|unauthenticated' website/docs/explanation/security-model.md | head
```
Expected: present in `docs/security.md`. If it is NOT present in the website page, it must be merged there before `docs/security.md` is deleted.

- [ ] **Step 2: Merge the AUTH.2 section into the website security page if missing**

Copy the section describing that nginx has no `auth_request`, that `/fhir/*` answers unauthenticated and returns identified data including an SSN identifier, that `api/auth.py` is the single enforcement point seeing only `/api/*`, and the four remediation options. Preserve the verification note (`/api/v1/configs` returns 401, `/fhir/Patient` returns 200, verified 2026-07-16).

- [ ] **Step 3: Diff every other retiring pair for unique content**

For each row in the retire table, run a section-heading comparison:
```bash
pair() { echo "=== $1 -> $2 ==="; diff <(grep -E '^#{2,3} ' "docs/$1" | sed 's/[#[:space:]]*//' | sort -u) \
                                       <(grep -E '^#{2,3} ' "website/docs/$2" | sed 's/[#[:space:]]*//' | sort -u); }
pair DEPLOYMENT.md how-to/deploy-docker.md
pair RUNBOOK.md how-to/operations-runbook.md
pair api-reference.md reference/api.md
pair architecture.md explanation/architecture.md
pair connector-integration.md how-to/integrate-connector.md
pair data-flow.md explanation/data-flow.md
pair policies.md explanation/policies.md
pair result-example.md reference/result-examples.md
pair scoring-system.md explanation/scoring-system.md
pair security.md explanation/security-model.md
pair user-manual.md how-to/use-the-web-ui.md
```
Lines prefixed `<` are headings only in `docs/`. Each one is either merged into the website page or consciously dropped as obsolete. Record the decision for each in the matrix file.

- [ ] **Step 4: Commit the merges**

```bash
git add website/docs docs/superpowers
git commit -m "docs: merge unique content from docs/ into canonical website pages"
```

### Task 2.3: Delete the retired files and repoint the entry points

**Files:**
- Delete: the 15 files in the retire table
- Modify: `README.md:240-249`, `docs/INDEX.md`

- [ ] **Step 1: Delete**

```bash
git rm docs/DEPLOYMENT.md docs/RUNBOOK.md docs/api-reference.md docs/architecture.md \
       docs/components.md docs/connector-integration.md docs/data-flow.md \
       docs/introduction.md docs/policies.md docs/result-example.md \
       docs/scoring-system.md docs/security.md docs/user-manual.md \
       docs/TECHNICAL_DOCUMENTATION.md
```

- [ ] **Step 2: Rewrite the README documentation table**

Replace the table at `README.md:240-249` with a pointer to the site plus the retained contributor artifacts. Keep the inline links at `README.md:106`, `129`, `217` and `232` working by repointing them at the corresponding website pages or removing them if the site path differs.

- [ ] **Step 3: Rewrite `docs/INDEX.md` as a thin contributor index**

It must list only: the website as canonical user documentation, `reference/env-vars.md`, `trust-gate/`, `quality-evaluation-methodology.md`, `superpowers/`, and a note that `internal/` is gitignored.

- [ ] **Step 4: Verify no dangling reference to a deleted file**

Run:
```bash
grep -rn 'docs/DEPLOYMENT\.md\|docs/RUNBOOK\.md\|docs/api-reference\.md\|docs/architecture\.md\|docs/components\.md\|docs/connector-integration\.md\|docs/data-flow\.md\|docs/introduction\.md\|docs/policies\.md\|docs/result-example\.md\|docs/scoring-system\.md\|docs/security\.md\|docs/user-manual\.md\|docs/TECHNICAL_DOCUMENTATION\.md' \
  --include='*.md' --include='*.py' --include='*.yml' --include='*.ts' --include='*.tsx' . \
  | grep -v node_modules | grep -v '^./docs/superpowers/'
```
Expected: no output. `CLAUDE.md` hits are fixed in Phase 6; if any appear here, note them for Task 6.1.

- [ ] **Step 5: Verify the site still builds**

Run:
```bash
cd website && npm run build 2>&1 | tail -5
```
Expected: a success line reporting the generated page count. Record that count; Phase 3 asserts it.

- [ ] **Step 6: Commit**

```bash
cd /home/developer/privacy-toolkit
git add -A docs README.md
git commit -m "docs: retire duplicated docs/ pages, make website the canonical home"
```

---

## Phase 3: Drift guard

### Task 3.1: Counted-claim checking

**Files:**
- Create: `scripts/check_docs.py`
- Test: `scripts/tests/test_check_docs.py`

**Interfaces:**
- Produces:
  - `count_config_profiles(root: pathlib.Path) -> int`
  - `count_compose_services(root: pathlib.Path) -> tuple[int, int, int, int]` returning `(total, always_on, opt_in, profiles)`
  - `check_counted_claims(root: pathlib.Path, docs: list[pathlib.Path]) -> list[str]` returning failure strings

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_check_docs.py`:

```python
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import check_docs

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_count_config_profiles_matches_disk():
    assert check_docs.count_config_profiles(ROOT) == 4


def test_count_compose_services_matches_compose():
    total, always_on, opt_in, profiles = check_docs.count_compose_services(ROOT)
    assert (total, always_on, opt_in, profiles) == (28, 15, 13, 7)


def test_counted_claim_failure_is_reported(tmp_path):
    doc = tmp_path / "bad.md"
    doc.write_text("There are 8 bundled profiles in the config directory.\n")
    failures = check_docs.check_counted_claims(ROOT, [doc])
    assert len(failures) == 1
    assert "8" in failures[0] and "4" in failures[0]


def test_counted_claim_success_is_silent(tmp_path):
    doc = tmp_path / "good.md"
    doc.write_text("There are 4 bundled profiles in the config directory.\n")
    assert check_docs.check_counted_claims(ROOT, [doc]) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```bash
python3 -m pytest scripts/tests/test_check_docs.py -q
```
Expected: FAIL with `ModuleNotFoundError: No module named 'check_docs'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/check_docs.py`:

```python
#!/usr/bin/env python3
"""Documentation drift check for MedAnon.

The env catalogue is the only documentation surface here that did not rot, and
it is the only one with a CI-gated drift check.  This script extends that
mechanism to the prose docs, for mechanical facts only.

Three checks
------------
- ``counted``  a number asserted in prose disagrees with the tree.
- ``path``     a repository path referenced in a doc does not exist.
- ``link``     a relative markdown link does not resolve.

Prose correctness is explicitly out of scope.  A human reads for that.

Usage
-----
    python3 scripts/check_docs.py          # check, exit 1 on failure
"""

from __future__ import annotations

import pathlib
import re
import sys

# Directories that hold documentation subject to these checks.
DOC_ROOTS = ("docs", "website/docs")
# Never checked: gitignored internal notes and generated output.
SKIP_PARTS = {"internal", "node_modules", "build", ".docusaurus"}


def count_config_profiles(root: pathlib.Path) -> int:
    return len(list((root / "services/anonymizer/config").glob("*.yaml")))


def count_compose_services(root: pathlib.Path) -> tuple[int, int, int, int]:
    """Return (total, always_on, opt_in, profile_count) from docker-compose.yml.

    Parsed with a minimal reader rather than PyYAML: the scripts here carry no
    third-party dependency, matching check_env.py.

    The ``in_profiles`` flag matters.  A naive reader that treats every
    ``      - x`` line as a profile also swallows networks, volumes, security_opt
    and the Traefik labels, which reports 48 profiles and 0 always-on services.
    Block-style list items count only while a ``profiles:`` header is open.
    """
    text = (root / "docker-compose.yml").read_text()
    body = text.split("\nservices:", 1)[1]
    for terminator in ("\nnetworks:", "\nvolumes:"):
        body = body.split(terminator, 1)[0]

    services: dict[str, list[str]] = {}
    current: str | None = None
    in_profiles = False
    for line in body.splitlines():
        name = re.match(r"^  ([a-z0-9][a-z0-9_-]*):\s*$", line)
        if name:
            current, in_profiles = name.group(1), False
            services[current] = []
            continue
        if current is None:
            continue
        inline = re.match(r"^    profiles:\s*\[(.*)\]", line)
        if inline:
            services[current] = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", inline.group(1))
            in_profiles = False
            continue
        if re.match(r"^    profiles:\s*$", line):
            in_profiles = True
            continue
        if in_profiles:
            item = re.match(r"^      -\s*[\"']?([A-Za-z][A-Za-z0-9_-]*)", line)
            if item:
                services[current].append(item.group(1))
            else:
                in_profiles = False

    always = [s for s, p in services.items() if not p]
    profiles = {p for ps in services.values() for p in ps}
    return len(services), len(always), len(services) - len(always), len(profiles)


# (regex with one capturing group, human label, callable returning the truth)
COUNTED_CLAIMS = (
    (r"(\d+)\s+bundled (?:YAML )?profiles", "bundled config profiles",
     lambda root: count_config_profiles(root)),
    (r"(\d+) services:", "compose services",
     lambda root: count_compose_services(root)[0]),
    (r"(\d+) always-on", "always-on services",
     lambda root: count_compose_services(root)[1]),
    (r"(\d+) opt-in", "opt-in services",
     lambda root: count_compose_services(root)[2]),
)


def check_counted_claims(root: pathlib.Path, docs: list[pathlib.Path]) -> list[str]:
    failures: list[str] = []
    for doc in docs:
        text = doc.read_text(errors="replace")
        for pattern, label, truth in COUNTED_CLAIMS:
            expected = truth(root)
            for match in re.finditer(pattern, text):
                claimed = int(match.group(1))
                if claimed != expected:
                    line = text[: match.start()].count("\n") + 1
                    failures.append(
                        f"counted  {doc}:{line}  claims {claimed} {label}, tree has {expected}"
                    )
    return failures


def iter_docs(root: pathlib.Path) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for doc_root in DOC_ROOTS:
        base = root / doc_root
        if not base.is_dir():
            continue
        for path in base.rglob("*.md*"):
            if SKIP_PARTS & set(path.parts):
                continue
            out.append(path)
    return sorted(out)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    docs = iter_docs(root)
    failures = check_counted_claims(root, docs)
    for failure in failures:
        print(failure)
    print(f"docs: {len(docs)} checked, {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
python3 -m pytest scripts/tests/test_check_docs.py -q
```
Expected: 4 passed.

- [ ] **Step 5: Un-ignore the new script**

`scripts/*` is gitignored at `.gitignore:180`. A new script is invisible to git
until a negation is added, and CI will invoke this one, so it must be tracked.
This mirrors `!scripts/check_env.py` at `.gitignore:183`.

Add directly below that line:
```
!scripts/check_docs.py
```

`scripts/tests/` stays untracked. It is ignored by both `scripts/*` and
`**/tests/`, matching the decided policy and the `packages/medanon-core/tests`
precedent: the tests run through `make ci-local`, not in CI. Do not use
`git add -f` on it.

- [ ] **Step 6: Verify the script is now trackable and the tests are not**

Run:
```bash
git check-ignore -v scripts/check_docs.py || echo "check_docs.py trackable"
git check-ignore -v scripts/tests/test_check_docs.py >/dev/null && echo "tests correctly ignored"
```
Expected: `check_docs.py trackable` and `tests correctly ignored`.

- [ ] **Step 7: Commit**

```bash
git add .gitignore scripts/check_docs.py
git commit -m "feat(scripts): add counted-claim drift check for documentation"
```

### Task 3.2: Path-existence and link checking

**Files:**
- Modify: `scripts/check_docs.py`
- Test: `scripts/tests/test_check_docs.py`

**Interfaces:**
- Consumes: `iter_docs`, `check_counted_claims` from Task 3.1.
- Produces:
  - `check_paths(root: pathlib.Path, docs: list[pathlib.Path]) -> list[str]`
  - `check_links(root: pathlib.Path, docs: list[pathlib.Path]) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_check_docs.py`:

```python
def test_missing_repo_path_is_reported(tmp_path):
    doc = tmp_path / "paths.md"
    doc.write_text("See `services/anonymizer/src/does_not_exist.py` for details.\n")
    failures = check_docs.check_paths(ROOT, [doc])
    assert len(failures) == 1
    assert "does_not_exist.py" in failures[0]


def test_existing_repo_path_is_silent(tmp_path):
    doc = tmp_path / "paths.md"
    doc.write_text("See `services/anonymizer/src/pipeline/processor.py` for details.\n")
    assert check_docs.check_paths(ROOT, [doc]) == []


def test_broken_relative_link_is_reported(tmp_path):
    doc = tmp_path / "links.md"
    doc.write_text("[gone](./nowhere.md)\n")
    failures = check_docs.check_links(ROOT, [doc])
    assert len(failures) == 1
    assert "nowhere.md" in failures[0]


def test_resolving_relative_link_is_silent(tmp_path):
    (tmp_path / "target.md").write_text("# target\n")
    doc = tmp_path / "links.md"
    doc.write_text("[here](./target.md)\n")
    assert check_docs.check_links(ROOT, [doc]) == []
```

- [ ] **Step 2: Run to verify they fail**

Run:
```bash
python3 -m pytest scripts/tests/test_check_docs.py -q
```
Expected: FAIL with `AttributeError: module 'check_docs' has no attribute 'check_paths'`.

- [ ] **Step 3: Implement both checks**

Add to `scripts/check_docs.py` above `main`:

```python
# A backticked token is treated as a repository path when it starts with one of
# these roots.  Anything else in backticks is code, not a path.
PATH_ROOTS = (
    "services/", "packages/", "client/", "scripts/", "helm/", "monitoring/",
    "docs/", "website/", "bench/",
)
PATH_RE = re.compile(r"`([A-Za-z0-9_./-]+)`")
LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?:|mailto:|#)([^)#]+)(?:#[^)]*)?\)")


def check_paths(root: pathlib.Path, docs: list[pathlib.Path]) -> list[str]:
    failures: list[str] = []
    for doc in docs:
        text = doc.read_text(errors="replace")
        for match in PATH_RE.finditer(text):
            token = match.group(1)
            if not token.startswith(PATH_ROOTS):
                continue
            # Trailing slash means a directory; strip it before resolving.
            candidate = root / token.rstrip("/")
            if candidate.exists():
                continue
            line = text[: match.start()].count("\n") + 1
            failures.append(f"path     {doc}:{line}  references missing {token}")
    return failures


def check_links(root: pathlib.Path, docs: list[pathlib.Path]) -> list[str]:
    failures: list[str] = []
    for doc in docs:
        text = doc.read_text(errors="replace")
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith("/"):
                continue
            if not (doc.parent / target).resolve().exists():
                line = text[: match.start()].count("\n") + 1
                failures.append(f"link     {doc}:{line}  unresolved {target}")
    return failures
```

Then replace the body of `main` so it runs all three:

```python
def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    docs = iter_docs(root)
    failures = (
        check_counted_claims(root, docs)
        + check_paths(root, docs)
        + check_links(root, docs)
    )
    for failure in failures:
        print(failure)
    print(f"docs: {len(docs)} checked, {len(failures)} failure(s)")
    return 1 if failures else 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
python3 -m pytest scripts/tests/test_check_docs.py -q
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

`scripts/tests/` is gitignored by design and is not committed.

```bash
git add scripts/check_docs.py
git commit -m "feat(scripts): add path-existence and link resolution doc checks"
```

### Task 3.3: Capture the drift queue and wire into CI

**Files:**
- Modify: `Makefile`, `.github/workflows/ci.yml`
- Create: `docs/superpowers/specs/2026-07-27-doc-drift-queue.md`

**Interfaces:**
- Consumes: `scripts/check_docs.py` from Tasks 3.1 and 3.2.
- Produces: the Phase 4 work queue.

- [ ] **Step 1: Run the checker against the tree and capture every failure**

Run:
```bash
python3 scripts/check_docs.py | tee docs/superpowers/specs/2026-07-27-doc-drift-queue.md
```
This file is the Phase 4 work queue. Do not fix anything yet.

- [ ] **Step 2: Verify the checker catches a deliberate break**

Run:
```bash
printf '\nThere are 99 bundled profiles.\n' >> docs/INDEX.md
python3 scripts/check_docs.py | grep 'claims 99'
git checkout -- docs/INDEX.md
```
Expected: a line reporting `claims 99 bundled config profiles, tree has 4`. If nothing is reported, the regex is wrong; fix it before continuing.

- [ ] **Step 3: Add the Makefile target**

Add after the `env-check` target:

```makefile
docs-check:
	@$(PYBIN) scripts/check_docs.py
```

And insert into `ci-local`, immediately after the `env catalogue up to date` block:

```makefile
	@echo "── doc drift ───────────────────────────────────────────────"
	@$(PYBIN) scripts/check_docs.py
	@echo "── doc check unit tests ────────────────────────────────────"
	@$(PYBIN) -m pytest scripts/tests -q
```

- [ ] **Step 4: Add the CI step**

In `.github/workflows/ci.yml`, in the `lint-python` job, after the `Env catalogue is up to date` step:

```yaml
      - name: Doc drift
        run: python3 scripts/check_docs.py
```

- [ ] **Step 5: Verify the target runs**

Run:
```bash
make docs-check
```
Expected: the same failure list as Step 1, exit status 1 until Phase 4 clears it.

- [ ] **Step 6: Commit**

```bash
git add Makefile .github/workflows/ci.yml docs/superpowers/specs/2026-07-27-doc-drift-queue.md
git commit -m "ci: gate documentation drift alongside the env catalogue"
```

---

## Phase 4: Documentation accuracy

### Task 4.1: Clear the mechanical drift queue

**Files:**
- Modify: whichever files `docs/superpowers/specs/2026-07-27-doc-drift-queue.md` names

- [ ] **Step 1: Work the queue top to bottom**

For each `counted` failure, re-derive the true number with the command in the baseline table and correct the prose. For each `path` failure, either fix the path or delete the reference if the file is genuinely gone. For each `link` failure, repoint or remove.

- [ ] **Step 2: Verify the queue is empty**

Run:
```bash
python3 scripts/check_docs.py
```
Expected: `0 failure(s)` and exit status 0.

- [ ] **Step 3: Commit**

```bash
git add -A docs website/docs
git commit -m "docs: correct counted claims, dead paths and broken links"
```

### Task 4.2: Read each retained document against the code

**Files:**
- Modify: `docs/INDEX.md`, `docs/quality-evaluation-methodology.md`, `docs/trust-gate/` (7 files), and the `website/docs/` pages that absorbed merged content

- [ ] **Step 1: Verify the Trust Gate docs match the service**

The mechanical checker cannot see that a described module was renamed. Compare each guide against the real layering:
```bash
ls services/trust-gate/src
sed -n '1,60p' services/trust-gate/ARCHITECTURE.md
```
Confirm the pipeline order described in the docs (`api/` to `engine.py` to `verdict/` to `checks/` to the domain model) matches the directory listing. Correct any guide that describes a module that no longer exists.

- [ ] **Step 2: Verify the scoring description matches medanon-core**

Run:
```bash
ls packages/medanon-core/src/scoring
ls services/anonymizer/src/pipeline/scoring
```
Expected: the engine lives in `packages/medanon-core/src/scoring/`; `pipeline/scoring/` holds only `gate.py` and `audit.py`. Correct any document still describing a single in-service scoring engine.

- [ ] **Step 3: Verify the profile-selection prose**

Run:
```bash
grep -rn 'config_gpas\|auto-selected' website/docs docs --include='*.md*' | grep -v superpowers
```
Expected: no reference to `config_gpas.yaml`, which this branch deleted. Correct any that remain.

- [ ] **Step 4: Re-run the guard**

Run:
```bash
make docs-check
```
Expected: `0 failure(s)`.

- [ ] **Step 5: Verify the site still builds**

Run:
```bash
cd website && npm run build 2>&1 | tail -5
```
Expected: success, page count matching what Task 2.3 Step 5 recorded, minus any pages deliberately removed.

- [ ] **Step 6: Commit**

```bash
cd /home/developer/privacy-toolkit
git add -A docs website/docs
git commit -m "docs: reconcile trust-gate, scoring and profile prose with the code"
```

---

## Phase 5: Environment

### Task 5.1: Remove dead keys and declare the posture flags

**Files:**
- Modify: `.env`, `.env.example`

- [ ] **Step 1: Re-confirm the dead list against the live file**

Run:
```bash
python3 scripts/check_env.py --env .env
```
Expected: the `keys nothing reads (7)` block listing `BULKHEAD_AI_MAX_CONCURRENT`, `BULKHEAD_ANALYTICS_MAX_CONCURRENT`, `BULKHEAD_SCORING_MAX_CONCURRENT`, `KEYCLOAK_PORT`, `MEDANON_RSA_PRIVATE_KEY`, `MEDANON_RSA_PUBLIC_KEY`, `SUBSCRIPTION_WEBHOOK_TIMEOUT`.

If the list differs from these 7, use the tool's output, not this plan. The tool is authoritative.

- [ ] **Step 2: Delete the dead keys from `.env`**

Remove exactly those key lines. Leave surrounding comments that still explain a live key.

- [ ] **Step 3: Declare the 11 posture flags explicitly**

Add a clearly headed block to `.env` setting each flag to its current effective value, so the security posture is reviewable in the file operators edit rather than inherited silently from compose:

```
# ── Privacy posture (declared explicitly, previously inherited from compose) ──
# These were unset and taking the docker-compose.yml default. Declaring them
# means a posture change shows up as a diff in this file.
MEDANON_REGULATED_MODE=false
MEDANON_OUTPUT_GATE_ENABLED=true
MEDANON_GATE_IDENTIFIER_MODE=block
MEDANON_PII_GATE_BLOCK_SEVERITY=critical,high
MEDANON_PII_GATE_NER_MODE=warn
MEDANON_HASH_ALLOW_PLAIN=false
MEDANON_NLP_FAIL_MODE=redact
MEDANON_RULE_SCHEMA_STRICT=false
MEDANON_REQUIRE_DURABLE_STORE=false
MEDANON_ALLOW_SQLITE_FALLBACK=false
MEDANON_AI_REQUIRE_LOCAL=true
```

`MEDANON_AI_REQUIRE_LOCAL` showed `<code default>` rather than a compose default. Before writing `true`, confirm the code default:
```bash
grep -rn 'MEDANON_AI_REQUIRE_LOCAL' services/anonymizer/src | head
```
Write whatever the code actually defaults to, not what this plan guesses.

- [ ] **Step 4: Verify**

Run:
```bash
python3 scripts/check_env.py --env .env
```
Expected: no `keys nothing reads` block, and no `posture flags you never set` block.

- [ ] **Step 5: Verify `.env.example` is still clean**

Run:
```bash
python3 scripts/check_env.py
```
Expected: `0 duplicate, 0 dead, 0 inert, 0 missing, 0 drift`.

If declaring a posture flag introduced a `missing` finding, add that flag to `.env.example` with an explanatory comment. Do not remove anything from `.env.example`.

### Task 5.2: Group the remaining overrides

**Files:**
- Modify: `.env`

- [ ] **Step 1: Reorder into labelled sections**

Group the remaining keys under headers mirroring the section names in `docs/reference/env-vars.md`, so the two files can be read side by side. Do not change any value.

- [ ] **Step 2: Verify no value changed**

Run:
```bash
git stash && grep -oE '^[A-Z_0-9]+=.*' .env | sort > /tmp/env-before.txt && git stash pop
grep -oE '^[A-Z_0-9]+=.*' .env | sort > /tmp/env-after.txt
diff /tmp/env-before.txt /tmp/env-after.txt
```
Expected: only the 7 deletions and the 11 additions. No other line differs.

`.env` is gitignored, so `git stash` will not capture it. If the stash approach does not apply, take the before-snapshot BEFORE editing in Step 1 instead.

- [ ] **Step 3: Verify the stack still starts**

Run:
```bash
make preflight
```
Expected: passes. This exercises the reordered `.env` without a full stack bring-up.

- [ ] **Step 4: Regenerate the env catalogue**

Run:
```bash
make env-docs
git diff --stat docs/reference/env-vars.md
```
If it changed, commit the regenerated file.

- [ ] **Step 5: Commit**

`.env` itself is gitignored and is not committed. Commit only `.env.example` and the regenerated catalogue if they changed.

```bash
git add .env.example docs/reference/env-vars.md
git commit -m "chore(env): declare privacy posture flags explicitly, drop dead keys"
```

---

## Phase 6: CLAUDE.md and Claude memory

### Task 6.1: Correct and re-verify CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Fix the known profile error**

`CLAUDE.md:83` and `CLAUDE.md:156` both claim 8 bundled profiles. Four exist. Line 156 also names `config_gpas.yaml`, deleted on this branch. Correct both to describe the 4 real profiles (`config.yaml`, `config_gdpr_eu.yaml`, `config_hipaa_safe_harbor.yaml`, `config_value_masking.yaml`) and the real selection behaviour.

- [ ] **Step 2: Verify every countable claim**

Run:
```bash
python3 scripts/check_docs.py 2>&1 | grep CLAUDE || echo "CLAUDE.md has no counted drift"
```
`CLAUDE.md` sits at the repo root, outside `DOC_ROOTS`. Either add the repo root's `CLAUDE.md` to `DOC_ROOTS` in `check_docs.py` and re-run its tests, or check it manually against the baseline table. Adding it is preferred, because it is the file most likely to drift.

- [ ] **Step 3: Repoint documentation references retired in Phase 2**

Run:
```bash
grep -n 'docs/[A-Za-z_-]*\.md' CLAUDE.md
```
Every hit must resolve. Repoint the ones Phase 2 deleted at their canonical website page.

- [ ] **Step 4: Verify the compose description is still correct**

Run:
```bash
python3 -c "
import sys; sys.path.insert(0,'scripts'); import check_docs, pathlib
print(check_docs.count_compose_services(pathlib.Path('.')))
print(check_docs.count_config_profiles(pathlib.Path('.')))
"
```
Expected: `(28, 15, 13, 7)` and `4`. The 28/15/13/7 sentence at `CLAUDE.md:171` is already correct and must stay correct.

- [ ] **Step 5: Verify and commit**

Run:
```bash
make docs-check
```
Expected: `0 failure(s)`.

`CLAUDE.md` is gitignored, so there is nothing to commit for it. `scripts/tests/`
is gitignored too. Commit only `scripts/check_docs.py` if Step 2 changed it.

```bash
git add scripts/check_docs.py
git commit -m "feat(scripts): include CLAUDE.md in the documentation drift check"
```

### Task 6.2: Consolidate Claude memory

**Files:**
- Modify: `/home/developer/.claude/projects/-home-developer-privacy-toolkit/memory/MEMORY.md`
- Delete and merge: files under the same directory

- [ ] **Step 1: List what exists**

Run:
```bash
ls -1 /home/developer/.claude/projects/-home-developer-privacy-toolkit/memory/*.md | wc -l
```
Expected: 56.

- [ ] **Step 2: Merge these clusters**

| New file | Absorbs |
|---|---|
| `project_bulk_export.md` | `project_bulk_export_native.md`, `project_bulk_export_bottleneck_gpas.md`, `project_bulk_export_ops_gotchas.md` |
| `project_gpas_operations.md` | `project_gpas_concurrency_limit.md`, `project_gpas_datasource_mysql_default.md`, `project_gpas_sqls_pg_mysql_never_ported.md` |
| `project_trust_gate.md` | `project_trust_gate_platform.md`, `project_trust_gate_accuracy_audit.md`, `project_trust_gate_validator.md`, `project_trust_gate_performance.md` |
| `project_pipeline_performance.md` | `project_pipeline_scaling_measured.md`, `project_stream_process_pool.md`, `project_text_id_map_hoist_fix.md`, `project_nlp_batch_tokenize_fix.md`, `project_benchmark_robustness_program.md` |

Each merged file keeps the frontmatter shape (`name`, `description`, `metadata.type`) and links related memories with `[[name]]`.

- [ ] **Step 3: Delete superseded memories**

`project_dep_refactor.md` describes the refactor that Phase 0 lands. Once landed, its forward-looking content is spent; keep only any durable trap it records, folded into `project_overview.md`, then delete the file.

Re-check `project_live_bench_findings.md` against the code before keeping it:
```bash
grep -rn 'circuit' services/anonymizer/src/integrations/trust_gate/ | head
```
If the three bugs it lists are fixed, delete it. If not, keep it and correct any stale `file:line`.

- [ ] **Step 4: Verify every retained memory's file references still resolve**

For each retained memory that names a path, confirm it exists. A memory asserting a path deleted in Phase 2 or Phase 0 is worse than no memory.

- [ ] **Step 5: Rewrite MEMORY.md**

The index must have exactly one line per file on disk, in the existing grouped format, with no orphan entries and no unlisted files.

- [ ] **Step 6: Verify the index matches the directory**

Run:
```bash
cd /home/developer/.claude/projects/-home-developer-privacy-toolkit/memory
comm -3 <(ls -1 *.md | grep -v '^MEMORY.md$' | sort) \
        <(grep -oE '\]\([a-z_0-9]+\.md\)' MEMORY.md | tr -d '](\)' | sort -u)
```
Expected: no output. Any line means a file is unlisted or an index entry points at a missing file.

---

## Final gate

- [ ] **Step 1: Full verification sweep**

Run:
```bash
cd /home/developer/privacy-toolkit
make ci-local
make docs-check
python3 scripts/check_env.py
python3 scripts/check_env.py --env .env
cd website && npm run build 2>&1 | tail -3
```

Expected, in order: `ci-local: all green`; `0 failure(s)`; `0 duplicate, 0 dead, 0 inert, 0 missing, 0 drift`; no dead-key and no unset-posture block; a successful site build.

- [ ] **Step 2: Confirm the tree is clean**

Run:
```bash
cd /home/developer/privacy-toolkit
git status --porcelain
```
Expected: empty, or only gitignored files.

- [ ] **Step 3: Report honestly**

State which gates passed with their actual output. If any phase was skipped or partially completed, say so explicitly and why. Do not report completion unless every gate above passed.
