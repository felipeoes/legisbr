"""Audit installed Python distribution licenses for core and optional extras.

Uses only the Python standard library (`importlib.metadata`) so it can run
inside minimal isolated wheel environments during CI.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import sys
from dataclasses import dataclass

# Strict permissive SPDX allowlist enforced for core installs (`--mode core`).
CORE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "MIT",
        "MIT-CMU",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "ISC",
        "PSF-2.0",
        "Zlib",
        "Unicode-3.0",
        "CDLA-Permissive-2.0",
        "0BSD",
        "MIT-0",
        "Unlicense",
        "CC0-1.0",
    }
)

# Permissive + weak file-level copyleft (MPL-2.0) allowlist enforced for extras (`--mode extras`).
# Weak file-level MPL-2.0 (e.g. certifi, tqdm pulled by OCR SDKs) and CNRI-Python
# (regex pulled by tiktoken) are permitted in optional extras; all copyleft and
# proprietary/unknown licenses are rejected.
EXTRAS_ALLOWLIST: frozenset[str] = CORE_ALLOWLIST | {"MPL-2.0", "CNRI-Python"}

# Documented manual overrides for distributions whose PyPI metadata omits a
# specific SPDX license identifier.
MANUAL_OVERRIDES: dict[str, str] = {
    # PyPI wheel leaves `License` blank and sets only generic `BSD License` classifier.
    # Upstream repository (mwilliamson/python-cobble) LICENSE file is BSD-2-Clause.
    "cobble": "BSD-2-Clause",
    # PyPI wheel bundles `LICENSE` (Apache-2.0) while omitting PEP 639 / core metadata `License` fields.
    "mistralai": "Apache-2.0",
    # PyPI wheel sets generic `License: BSD`; bundled LICENSE is BSD-3-Clause.
    "nodeenv": "BSD-3-Clause",
    # PyPI wheel sets generic `License: BSD`; bundled LICENSE.txt is BSD-2-Clause.
    "pyasn1-modules": "BSD-2-Clause",
    # PyPI wheel sets freeform `License: BSD-3-Clause, Apache-2.0, dependency licenses`.
    # pypdfium2 is dual-licensed Apache-2.0 OR BSD-3-Clause, and all bundled PDFium
    # BUILD_LICENSES are permissive (Apache-2.0, BSD-2/3-Clause, MIT, Zlib, IJG, Unicode-3.0, FTL).
    "pypdfium2": "Apache-2.0 OR BSD-3-Clause",
}

_CLASSIFIER_MAP: dict[str, str] = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: zlib/libpng License": "Zlib",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "License :: OSI Approved :: GNU General Public License (GPL)": "GPL",
    "License :: OSI Approved :: GNU General Public License v2 (GPLv2)": "GPL-2.0",
    "License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)": "GPL-3.0",
    "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
    "License :: OSI Approved :: GNU Affero General Public License v3": "AGPL-3.0",
    "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)": "AGPL-3.0-or-later",
    "License :: OSI Approved :: GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.0",
    "License :: OSI Approved :: GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
}

_ALIAS_MAP: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license (mit)": "MIT",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "asl 2": "Apache-2.0",
    "bsd": "BSD",
    "bsd license": "BSD",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "0bsd": "0BSD",
    "isc": "ISC",
    "isc license": "ISC",
    "psf": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "zlib": "Zlib",
    "unicode-3.0": "Unicode-3.0",
    "unicode-dfs-2016": "Unicode-3.0",
    "cdla-permissive-2.0": "CDLA-Permissive-2.0",
    "mit-0": "MIT-0",
    "unlicense": "Unlicense",
    "cc0-1.0": "CC0-1.0",
    "mpl-2.0": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "cnri-python": "CNRI-Python",
}

_COPYLEFT_TEXT_RE = re.compile(
    r"\b(?:gnu\s+affero|agpl|lgpl|gpl(?:v?[123])?|gnu\s+(?:lesser\s+|library\s+)?general\s+public|sspl|eupl|cpal)\b",
    re.IGNORECASE,
)


def normalize_pkg_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _normalize_atom(raw: str) -> str:
    cleaned = raw.strip().strip("()").strip()
    if not cleaned:
        return ""
    lowered = re.sub(r"\s+", " ", cleaned).lower()
    if lowered in _ALIAS_MAP:
        return _ALIAS_MAP[lowered]
    if _COPYLEFT_TEXT_RE.search(cleaned):
        upper = cleaned.upper()
        if "AFFERO" in upper or "AGPL" in upper:
            return "AGPL"
        if "LESSER" in upper or "LIBRARY" in upper or "LGPL" in upper:
            return "LGPL"
        if "SSPL" in upper:
            return "SSPL"
        if "EUPL" in upper:
            return "EUPL"
        if "CPAL" in upper:
            return "CPAL"
        return "GPL"
    return cleaned


def _extract_from_classifiers(classifiers: list[str]) -> str | None:
    found: list[str] = []
    for clf in classifiers:
        mapped = _CLASSIFIER_MAP.get(clf.strip())
        if mapped and mapped not in found:
            found.append(mapped)
        elif clf.startswith("License ::") and _COPYLEFT_TEXT_RE.search(clf):
            atom = _normalize_atom(clf)
            if atom not in found:
                found.append(atom)
    if not found:
        return None
    return " OR ".join(found)


def _extract_from_freeform(text: str) -> str | None:
    stripped = text.strip()
    if not stripped or stripped.lower() in {"unknown", "none", "dual license"}:
        return None
    # Multi-line full license texts (e.g. tiktoken, python-discovery).
    if "\n" in stripped or len(stripped) > 120:
        first_line = stripped.splitlines()[0].strip()
        if _COPYLEFT_TEXT_RE.search(first_line):
            return _normalize_atom(first_line)
        first_lower = first_line.lower()
        if (
            "mit license" in first_lower
            or "permission is hereby granted, free of charge" in stripped[:300].lower()
        ):
            return "MIT"
        if "apache license" in first_lower and "2.0" in first_lower:
            return "Apache-2.0"
        if "bsd 3-clause" in first_lower:
            return "BSD-3-Clause"
        if "bsd 2-clause" in first_lower:
            return "BSD-2-Clause"
        if _COPYLEFT_TEXT_RE.search(stripped[:400]):
            return _normalize_atom(stripped[:400])
        return first_line[:80]
    return stripped


@dataclass(frozen=True)
class AuditEntry:
    name: str
    version: str
    expression: str
    source: str


def resolve_license(dist: md.Distribution) -> AuditEntry:
    meta = dist.metadata
    raw_name = meta.get("Name") or "unknown"
    version = meta.get("Version") or "?"
    norm_name = normalize_pkg_name(raw_name)

    if norm_name in MANUAL_OVERRIDES:
        return AuditEntry(raw_name, version, MANUAL_OVERRIDES[norm_name], "override")

    expr = (meta.get("License-Expression") or "").strip()
    if expr and expr.upper() != "UNKNOWN":
        return AuditEntry(raw_name, version, expr, "License-Expression")

    classifiers = [
        c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")
    ]
    lic_raw = (meta.get("License") or "").strip()

    # Prefer classifiers over ambiguous placeholders or long pasted texts.
    if (
        not lic_raw
        or lic_raw.lower() in {"unknown", "dual license"}
        or len(lic_raw) > 120
    ):
        from_clf = _extract_from_classifiers(classifiers)
        if from_clf:
            return AuditEntry(raw_name, version, from_clf, "Classifier")

    from_lic = _extract_from_freeform(lic_raw)
    if from_lic:
        return AuditEntry(raw_name, version, from_lic, "License")

    from_clf = _extract_from_classifiers(classifiers)
    if from_clf:
        return AuditEntry(raw_name, version, from_clf, "Classifier")

    return AuditEntry(raw_name, version, "UNKNOWN", "missing")


def _split_top_level(expr: str, operator: str) -> list[str]:
    """Split an SPDX-like expression on top-level `OR` / `AND` operators."""
    parts: list[str] = []
    depth = 0
    start = 0
    pattern = re.compile(rf"\s+{operator}\s+", re.IGNORECASE)
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            match = pattern.match(expr, i)
            if match:
                parts.append(expr[start:i].strip())
                i = match.end()
                start = i
                continue
        i += 1
    parts.append(expr[start:].strip())
    return [p for p in parts if p]


def _strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        wraps_all = True
        for idx, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx < len(expr) - 1:
                    wraps_all = False
                    break
        if wraps_all and depth == 0:
            expr = expr[1:-1].strip()
        else:
            break
    return expr


def _is_allowed_by(expr: str, allowlist: frozenset[str]) -> bool:
    """True iff `expr` is satisfied strictly by `allowlist`."""
    expr = _strip_outer_parens(expr)
    if not expr or expr == "UNKNOWN":
        return False
    or_branches = _split_top_level(expr, "OR")
    if len(or_branches) > 1:
        return any(_is_allowed_by(branch, allowlist) for branch in or_branches)
    and_branches = _split_top_level(expr, "AND")
    if len(and_branches) > 1:
        return all(_is_allowed_by(branch, allowlist) for branch in and_branches)
    atom = _normalize_atom(expr)
    return atom in allowlist


def is_allowed_core(expr: str) -> bool:
    """True iff `expr` is satisfied strictly by CORE_ALLOWLIST."""
    return _is_allowed_by(expr, CORE_ALLOWLIST)


def is_allowed_extras(expr: str) -> bool:
    """True iff `expr` is satisfied strictly by EXTRAS_ALLOWLIST (permissive + MPL-2.0)."""
    return _is_allowed_by(expr, EXTRAS_ALLOWLIST)


def audit_environment(
    *, mode: str, exclude: set[str]
) -> tuple[list[AuditEntry], list[AuditEntry]]:
    entries: list[AuditEntry] = []
    seen: set[str] = set()
    for dist in md.distributions():
        entry = resolve_license(dist)
        norm = normalize_pkg_name(entry.name)
        if norm in exclude or norm in seen:
            continue
        seen.add(norm)
        entries.append(entry)
    entries.sort(key=lambda e: e.name.lower())

    checker = is_allowed_core if mode == "core" else is_allowed_extras
    violations = [entry for entry in entries if not checker(entry.expression)]
    return entries, violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("core", "extras"),
        default="core",
        help="core: strict permissive allowlist; extras: permissive + MPL-2.0 allowlist",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Distribution name to exclude (repeatable or comma-separated)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print all inspected distributions and resolved license expressions",
    )
    args = parser.parse_args(argv)

    excluded: set[str] = set()
    for item in args.exclude:
        for name in item.split(","):
            if name.strip():
                excluded.add(normalize_pkg_name(name))

    entries, violations = audit_environment(mode=args.mode, exclude=excluded)

    if args.verbose:
        for entry in entries:
            status = "FAIL" if entry in violations else "OK  "
            print(
                f"{status} {entry.name:30s} {entry.version:12s} "
                f"{entry.expression} ({entry.source})"
            )

    if violations:
        print(
            f"\nLicense audit FAILED ({args.mode} mode): "
            f"{len(violations)}/{len(entries)} offending distribution(s):",
            file=sys.stderr,
        )
        for entry in violations:
            print(
                f"  - {entry.name}=={entry.version}: {entry.expression!r} "
                f"(from {entry.source})",
                file=sys.stderr,
            )
        return 1

    print(
        f"License audit PASSED ({args.mode} mode): "
        f"{len(entries)} distributions verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
