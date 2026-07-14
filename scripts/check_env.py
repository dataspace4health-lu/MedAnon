#!/usr/bin/env python3
"""Environment-variable drift check for MedAnon.

The env surface rotted because nothing compared it to the code.  This script is
that comparison, and it is meant to run in CI.

Three sources of truth, in the order that decides what actually runs:

1. ``docker-compose.yml`` -- ``${VAR:-default}`` is what ``make up`` deploys.
2. ``.env.example``       -- what an operator copies to ``.env``.
3. the code               -- ``os.environ.get(VAR, default)`` is the library
                             fallback for non-Docker use (tests, CLI, Helm).

Checks
------
- ``duplicate``   a key assigned twice in ``.env.example`` (the second wins,
                  silently).
- ``dead``        a key in ``.env.example`` that nothing reads.
- ``missing``     a behaviour-gating flag absent from ``.env.example``.
- ``drift``       ``.env.example`` disagrees with the compose default.  Annotate
                  a deliberate divergence with ``# env-check: differs <reason>``
                  on the line above.
- ``interp``      an unescaped ``$`` in a value.  Compose expands it: a value of
                  ``/{type}/$validate`` reaches the container as ``/{type}/``.

Usage
-----
    python3 scripts/check_env.py              # check, exit 1 on failure
    python3 scripts/check_env.py --docs PATH  # regenerate the full catalogue
    python3 scripts/check_env.py --env .env   # also diff a real .env
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Flags that change behaviour rather than tuning.  Absent from .env.example, an
# operator has no way to discover them; several are fail-closed switches.
POSTURE_FLAGS = frozenset(
    {
        "MEDANON_REGULATED_MODE",
        "MEDANON_OUTPUT_GATE_ENABLED",
        "MEDANON_PII_GATE",
        "MEDANON_PII_GATE_BLOCK_SEVERITY",
        "MEDANON_PII_GATE_NER_MODE",
        "MEDANON_GATE_IDENTIFIER_MODE",
        "MEDANON_MANIFEST_ENABLED",
        "MEDANON_SCORING_ENABLED",
        "MEDANON_RULE_SCHEMA_STRICT",
        "MEDANON_NLP_FAIL_MODE",
        "MEDANON_REQUIRE_DURABLE_STORE",
        "MEDANON_ALLOW_SQLITE_FALLBACK",
        "MEDANON_HASH_KEY",
        "MEDANON_HASH_ALLOW_PLAIN",
        "MEDANON_AI_REQUIRE_LOCAL",
        "MEDANON_AI_PII_REQUIRE_LOCAL",
        "TRUST_GATE_MODE",
    }
)

# A *consumer* reads the variable and acts on it.
CONSUMER_GLOBS = ("scripts/*.sh", "Makefile")
# A *setter* merely assigns it for some container. Being set in a Helm values
# file proves nothing about whether any code path reads it.
SETTER_GLOBS = ("helm/**/*.yaml", "helm/**/*.yml")

_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_ANNOTATION = re.compile(r"#\s*env-check:\s*differs")
# Unescaped $ not part of ${...}
_BARE_DOLLAR = re.compile(r"(?<!\$)\$(?!\{)")


# ---------------------------------------------------------------------------
# Source 3: the code
# ---------------------------------------------------------------------------


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        return str(node.value)
    return None


_VARLIKE = re.compile(r"[A-Z][A-Z0-9_]{3,}")


def code_vars() -> tuple[dict[str, set[str]], set[str]]:
    """``({VAR: {default, ...}}, {any var-like literal})`` from services/.

    Uses the AST rather than a regex so multi-line calls are not missed -- the
    reason an earlier hand audit reported ``MEDANON_STRUCTURAL_PHI_ENABLED`` as
    dead when ``action_dispatcher`` reads it across three lines.

    The second set is every var-shaped *string literal* anywhere in the source.
    It exists because some variables never appear inside an ``os.environ`` call:
    ``pool_budget`` passes the name to a helper (``_budget("proxy",
    "PROXY_POOL_SIZE")``), and the Trust Gate reads several through its own
    ``_env_int(...)`` wrapper. Treating those literals as references keeps the
    ``dead`` check conservative -- a false negative here is a stale line in a
    file, a false positive is a working knob deleted.
    """
    found: dict[str, set[str]] = {}
    literals: set[str] = set()
    for path in (ROOT / "services").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _VARLIKE.fullmatch(node.value):
                    literals.add(node.value)
            name = default = None
            if isinstance(node, ast.Call):
                fn = node.func
                is_get = (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in ("get", "getenv")
                    and node.args
                )
                if is_get:
                    name = _literal(node.args[0])
                    if len(node.args) > 1:
                        default = _literal(node.args[1])
            elif isinstance(node, ast.Subscript):
                value = node.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "environ"
                    and isinstance(node.slice, ast.Constant)
                ):
                    name = _literal(node.slice)
            if name and re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                found.setdefault(name, set())
                if default is not None:
                    found[name].add(default)
    return found, literals


def dynamic_vars() -> set[str]:
    """Names the code builds at runtime, which the AST pass cannot see.

    ``utils.bulkhead`` derives ``BULKHEAD_<NAME>_MAX_CONCURRENT`` from its
    argument, so only the upstreams actually wrapped in a ``bulkhead(...)`` call
    have a real knob.  Everything else in that family is inert.
    """
    names: set[str] = set()
    for path in (ROOT / "services").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        for upstream in re.findall(r"bulkhead\(\s*\"([a-z_]+)\"", text, re.S):
            names.add(f"BULKHEAD_{upstream.upper()}_MAX_CONCURRENT")
    return names


# ---------------------------------------------------------------------------
# Source 1: docker-compose.yml
# ---------------------------------------------------------------------------


def compose_vars() -> tuple[dict[str, str], set[str], set[str]]:
    """``({VAR: literal default}, {VAR compose demands}, {VAR whose default is computed})``.

    Handles all three interpolation forms:
      ``${VAR}``            -- passthrough; empty when unset
      ``${VAR:-default}``   -- defaulted
      ``${VAR:?message}``   -- compose *errors out* when unset

    A default containing a nested ``${...}`` (the app-db URL embeds
    ``${MEDANON_APP_DB_USER}``, the Redis URL embeds the password) is computed,
    not a literal, so there is nothing to compare against ``.env.example``.

    A variable can appear in several forms: ``MINIO_ROOT_USER`` has a ``:-``
    empty default in the shared anchor and a ``:?`` in the ``s3`` profile.  It is
    demanded, because enabling that profile hard-fails without it.
    """
    text = (ROOT / "docker-compose.yml").read_text()
    defaults: dict[str, str] = {}
    demanded: set[str] = set()
    computed: set[str] = set()
    bare: set[str] = set()
    for m in re.finditer(r"\$\{([A-Z_][A-Z0-9_]*)(:[-?])?([^}]*)\}", text):
        name, op, rest = m.group(1), m.group(2), m.group(3)
        if op == ":-":
            if "$" in rest:
                computed.add(name)
            else:
                defaults.setdefault(name, rest)
        elif op == ":?":
            demanded.add(name)
        else:
            bare.add(name)
    # A bare ``${VAR}`` with no literal default anywhere is the operator's job.
    demanded |= {n for n in bare if n not in defaults and n not in computed}
    return defaults, demanded, computed


# ---------------------------------------------------------------------------
# Source 2: .env.example (or any .env)
# ---------------------------------------------------------------------------


def _strip_inline_comment(value: str) -> str:
    """Drop a ``  # ...`` suffix, matching Compose's unquoted-value rule.

    Compose strips an inline comment only when it is preceded by whitespace, so
    ``PASS=medanon_dev  # change me`` yields ``medanon_dev`` but ``URL=a#b``
    keeps the ``#``.
    """
    if value.startswith(('"', "'")):
        return value
    m = re.search(r"\s+#", value)
    return value[: m.start()].strip() if m else value


def env_file(path: pathlib.Path) -> tuple[dict[str, str], list[str], set[str]]:
    """``({VAR: value}, [duplicate, ...], {VAR annotated as deliberate drift})``.

    The ``# env-check: differs`` marker may appear anywhere in the contiguous
    comment block immediately above an assignment, so an explanation can run to
    several lines without the marker having to be the last one.
    """
    values: dict[str, str] = {}
    duplicates: list[str] = []
    annotated: set[str] = set()
    block: list[str] = []
    for line in path.read_text().splitlines():
        m = _ENV_LINE.match(line)
        if not m:
            if line.lstrip().startswith("#"):
                block.append(line)
            else:
                block.clear()  # a blank line ends the block
            continue
        name = m.group(1)
        value = _strip_inline_comment(m.group(2).strip())
        if name in values:
            duplicates.append(name)
        values[name] = value
        if any(_ANNOTATION.search(c) for c in block):
            annotated.add(name)
        block.clear()
    return values, duplicates, annotated


def _names_in(globs: tuple[str, ...]) -> set[str]:
    names: set[str] = set()
    for pattern in globs:
        for path in ROOT.glob(pattern):
            if not path.is_file():
                continue
            names.update(
                re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", path.read_text(errors="ignore"))
            )
    return names


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def check() -> int:
    code, literals = code_vars()
    dynamic = dynamic_vars()
    comp_defaults, comp_demanded, comp_computed = compose_vars()
    declared, duplicates, annotated = env_file(ROOT / ".env.example")
    consumers = _names_in(CONSUMER_GLOBS)
    setters = _names_in(SETTER_GLOBS)

    # Read by something that acts on it: the Python code, compose interpolation
    # (which is how third-party images such as Postgres and Keycloak receive
    # their settings), or the Makefile / shell scripts.
    consumed = (
        set(code)
        | literals
        | dynamic
        | set(comp_defaults)
        | comp_demanded
        | comp_computed
        | consumers
    )
    failures: list[str] = []
    reported: set[str] = set()

    for name in sorted(set(duplicates)):
        failures.append(
            f"duplicate  {name}: assigned more than once; the last wins silently"
        )

    # The bulkhead knob only exists for upstreams actually wrapped in a
    # ``bulkhead(...)`` call; ``utils.bulkhead`` derives the name from the
    # argument. Everything else in the family is a no-op, however many files
    # configure it -- so this rule overrides the generic checks below.
    for name in sorted(declared):
        if (
            re.fullmatch(r"BULKHEAD_[A-Z]+_MAX_CONCURRENT", name)
            and name not in dynamic
        ):
            reported.add(name)
            failures.append(
                f"inert      {name}: no bulkhead(...) call for this upstream "
                f"(real ones: {', '.join(sorted(dynamic)) or 'none'})"
            )

    for name in sorted(set(declared) - consumed - setters - reported):
        reported.add(name)
        failures.append(f"dead       {name}: declared but nothing reads it")

    # Set in a Helm values file for some container, but no code path reads it.
    for name in sorted((set(declared) & setters) - consumed - reported):
        failures.append(
            f"inert      {name}: set in Helm values but no code path reads it"
        )

    for name in sorted(POSTURE_FLAGS - set(declared)):
        failures.append(
            f"missing    {name}: behaviour-gating flag absent from .env.example"
        )

    for name in sorted(comp_demanded - set(declared)):
        failures.append(
            f"missing    {name}: compose demands it (${{{name}:?}}) and .env.example "
            f"does not declare it"
        )

    for name, value in sorted(declared.items()):
        if name in comp_defaults and name not in annotated:
            if value != comp_defaults[name]:
                failures.append(
                    f"drift      {name}: .env.example={value!r} but compose "
                    f"defaults to {comp_defaults[name]!r}"
                )
        if _BARE_DOLLAR.search(value):
            failures.append(
                f"interp     {name}: value {value!r} contains a bare '$'; compose "
                f"expands it (e.g. '/{{type}}/$validate' arrives as '/{{type}}/')"
            )

    if failures:
        print(f"env drift: {len(failures)} problem(s)\n", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nAnnotate a deliberate divergence with '# env-check: differs <reason>' "
            "on the line above the assignment.",
            file=sys.stderr,
        )
        return 1

    print(
        f"env OK: {len(declared)} declared in .env.example, {len(consumed)} readable "
        f"by code/compose/scripts; 0 duplicate, 0 dead, 0 inert, 0 missing, 0 drift"
    )
    return 0


def diff_env(path: pathlib.Path) -> int:
    """Report what a real ``.env`` is missing, and what in it nothing reads.

    Never fails: an operator's ``.env`` may legitimately override any of the
    ~400 catalogued variables.  The two things worth surfacing are a posture
    flag they never set (so it silently takes the compose default) and a key
    nothing reads (a typo, or a knob that was removed).
    """
    if not path.exists():
        print(f"\n{path} does not exist — nothing to diff")
        return 0

    code, literals = code_vars()
    dynamic = dynamic_vars()
    comp_defaults, comp_demanded, comp_computed = compose_vars()
    consumed = (
        set(code)
        | literals
        | dynamic
        | set(comp_defaults)
        | comp_demanded
        | comp_computed
        | _names_in(CONSUMER_GLOBS)
    )
    declared, _, _ = env_file(ROOT / ".env.example")
    actual, duplicates, _ = env_file(path)

    print(f"\n{path.name} vs .env.example + the code")
    print(f"  keys in {path.name}: {len(actual)}   .env.example: {len(declared)}")

    if duplicates:
        print(f"\n  duplicate keys (the last wins): {sorted(set(duplicates))}")

    unset_posture = sorted(POSTURE_FLAGS - set(actual))
    if unset_posture:
        print(
            f"\n  posture flags you never set ({len(unset_posture)}) — these take the "
            f"compose default:"
        )
        for name in unset_posture:
            fallback = comp_defaults.get(name, "<code default>")
            print(f"    {name:36s} -> {fallback}")

    unknown = sorted(set(actual) - consumed)
    if unknown:
        print(f"\n  keys nothing reads ({len(unknown)}) — typo, or a removed knob:")
        for name in unknown:
            print(f"    {name}")

    tuning = sorted((set(actual) - set(declared)) & consumed)
    if tuning:
        print(
            f"\n  {len(tuning)} further overrides, all valid "
            f"(see docs/reference/env-vars.md)"
        )
    return 0


def generate_docs(out: pathlib.Path) -> int:
    code, _ = code_vars()
    dynamic = dynamic_vars()
    comp_defaults, comp_demanded, comp_computed = compose_vars()
    declared, _, _ = env_file(ROOT / ".env.example")

    names = sorted(
        set(code) | dynamic | set(comp_defaults) | comp_demanded | comp_computed
    )
    groups: dict[str, list[str]] = {}
    for name in names:
        prefix = name.split("_")[0]
        groups.setdefault(prefix, []).append(name)

    lines = [
        "# Environment variables",
        "",
        "Generated by `scripts/check_env.py --docs`. Do not edit by hand.",
        "",
        "`.env.example` carries only what an operator must set or should consciously",
        "review. This is the exhaustive catalogue: every variable the code, or",
        "`docker-compose.yml`, actually reads.",
        "",
        "- **code default** -- the fallback in `os.environ.get(...)`; applies to tests,",
        "  the CLI, and Helm deployments.",
        "- **compose default** -- what `make up` deploys when the variable is unset.",
        "- **in .env.example** -- whether an operator sees it when copying the template.",
        "",
    ]
    for prefix in sorted(groups):
        lines += [
            f"## `{prefix}_*`",
            "",
            "| variable | code default | compose default | in .env.example |",
            "|---|---|---|---|",
        ]
        for name in groups[prefix]:
            cd = sorted(code.get(name, set()))
            cd_s = (
                f"`{cd[0]}`"
                if len(cd) == 1
                else ("varies" if len(cd) > 1 else "required")
            )
            if name in dynamic and name not in code:
                cd_s = "`0` (derived)"
            cm = comp_defaults.get(name)
            if cm:
                cm_s = f"`{cm}`"
            elif name in comp_demanded:
                cm_s = "**must be set**"
            elif name in comp_computed:
                cm_s = "computed"
            else:
                cm_s = "not set"
            ex_s = "yes" if name in declared else "no"
            lines.append(f"| `{name}` | {cd_s} | {cm_s} | {ex_s} |")
        lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    print(f"wrote {out} ({len(names)} variables)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", metavar="PATH", help="regenerate the full catalogue")
    ap.add_argument(
        "--env", metavar="PATH", help="also diff a real .env against the example"
    )
    args = ap.parse_args()

    if args.docs:
        return generate_docs(pathlib.Path(args.docs))

    rc = check()
    if args.env:
        diff_env(pathlib.Path(args.env))
    return rc


if __name__ == "__main__":
    sys.exit(main())
