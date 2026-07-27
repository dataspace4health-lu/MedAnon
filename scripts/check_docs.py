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
    failures = (
        check_counted_claims(root, docs)
        + check_paths(root, docs)
        + check_links(root, docs)
    )
    for failure in failures:
        print(failure)
    print(f"docs: {len(docs)} checked, {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
