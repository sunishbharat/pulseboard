"""Repository hygiene check (DESIGN §7.3).

Fails if tracked files (or the given files) contain AI attribution text or commands for a
package manager other than uv. With ``--commits RANGE`` it also checks commit messages.

Usage:
    uv run python scripts/check_hygiene.py [FILE ...] [--commits BASE..HEAD]
"""

import argparse
import codecs
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

ATTRIBUTION_RULE = "attribution"
PACKAGE_MANAGER_RULE = "package-manager"

# Stored ROT13-encoded so this file does not itself contain the terms it looks for.
_ENCODED_TERMS = (
    "pynhqr",
    "naguebcvp",
    "pungtcg",
    "bcranv",
    "pbcvybg",
    "trzvav",
    "nv-trarengrq",
    "nv trarengrq",
    "jevggra ol na nffvfgnag",
)
ATTRIBUTION_TERMS = tuple(codecs.decode(term, "rot13") for term in _ENCODED_TERMS)

_ATTRIBUTION = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in ATTRIBUTION_TERMS) + r")(?![a-z0-9])",
    re.IGNORECASE,
)
_PACKAGE_MANAGER = re.compile(
    r"\b(pip3?\s+(install|uninstall)|python3?\s+-m\s+pip|poetry\s+(add|install|lock)"
    r"|conda\s+install)\b",
    re.IGNORECASE,
)

SELF = "scripts/check_hygiene.py"
ALLOWED: dict[str, frozenset[str]] = {
    # The design states the rule itself, so it names the forbidden commands.
    "docs/DESIGN.md": frozenset({PACKAGE_MANAGER_RULE}),
    # The checker's tests need forbidden commands as test cases.
    "tests/test_check_hygiene.py": frozenset({PACKAGE_MANAGER_RULE}),
}


class Finding(NamedTuple):
    """One hygiene violation."""

    location: str
    rule: str
    text: str


def scan_text(location: str, text: str, allowed: frozenset[str] = frozenset()) -> list[Finding]:
    """Findings in ``text``, reported as ``location:line``."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in (
            (ATTRIBUTION_RULE, _ATTRIBUTION),
            (PACKAGE_MANAGER_RULE, _PACKAGE_MANAGER),
        ):
            if rule not in allowed and (match := pattern.search(line)):
                findings.append(Finding(f"{location}:{number}", rule, match.group(0)))
    return findings


def scan_files(paths: Iterable[str]) -> list[Finding]:
    """Findings in each readable text file, skipping this script and binary files."""
    findings: list[Finding] = []
    for raw in paths:
        path = Path(raw).as_posix()
        if path == SELF:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        findings.extend(scan_text(path, text, ALLOWED.get(path, frozenset())))
    return findings


def tracked_files() -> list[str]:
    """Files tracked by git (read-only)."""
    output = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [name for name in output.split("\0") if name]


def scan_commits(rev_range: str) -> list[Finding]:
    """Attribution findings in commit messages within ``rev_range`` (read-only)."""
    output = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x1e", rev_range],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    findings: list[Finding] = []
    for record in output.split("\x1e"):
        sha, _, message = record.strip().partition("\0")
        if sha:
            findings.extend(
                f for f in scan_text(f"commit {sha[:12]}", message) if f.rule == ATTRIBUTION_RULE
            )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check; return 1 if anything is found."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to check (default: all tracked files)")
    parser.add_argument("--commits", metavar="RANGE", help="also check commit messages")
    args = parser.parse_args(argv)

    findings = scan_files(args.files or tracked_files())
    if args.commits:
        findings.extend(scan_commits(args.commits))
    for finding in findings:
        sys.stderr.write(f"{finding.location}: {finding.rule}: {finding.text!r}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
