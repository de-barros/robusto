"""Flag tracked files that may carry a real credential.

A sensitive *name* is not a finding. `PROBE_TOKEN = "robusto-probe-ok"` and
`AUTH_SIGNATURES = (...)` are not secrets, and a check that flags them fires
hardest in exactly the files that handle authentication, which is where a real
secret would be least noticed among the noise. So the name only decides where
to look; the assigned *value* decides whether to report.

Two things count as a value worth reporting: a literal carrying a known
credential prefix, at any length, and an opaque literal long enough and mixed
enough to be a real key. Everything else, including every tuple, call, path,
placeholder and readable phrase, is left alone.

This cannot prove a file is clean. It is a cheap guard against the specific
accident of pasting a live key into tracked source.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


SENSITIVE_FILENAME_RE = re.compile(
    r"(?:^|[._/-])(?:api[_-]?key|token|secret|password|passwd|credential|auth)(?:[._/-]|$)",
    re.IGNORECASE,
)

#: NAME = "value" or NAME: "value", capturing the quoted literal if there is one.
ASSIGNMENT_RE = re.compile(
    r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(?:r?[bu]?)?(['"])(.*?)\2"""
)
SENSITIVE_PARTS = {"TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "KEY"}

#: Issuer prefixes that make a literal a credential whatever its length.
CREDENTIAL_PREFIXES = (
    "sk-", "sk_live_", "sk_test_", "rk_live_", "pk_live_",
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "xoxb-", "xoxp-", "xoxa-", "xapp-",
    "AKIA", "ASIA", "ya29.", "AIza", "glpat-", "dop_v1_", "shpat_",
)

#: Values that name a secret rather than being one.
PLACEHOLDER_RE = re.compile(
    r"(?:^$|\s|\{|\}|<|>|\$|\.\.\.|^(?:x{3,}|\*{3,})$"
    r"|changeme|placeholder|your[_-]|example|redacted|dummy|fake|sample|not[_-]?set)",
    re.IGNORECASE,
)

#: Below this, a literal is too short to be worth reporting on its own.
MIN_OPAQUE_LENGTH = 20

ALLOWLISTED_FILES = {
    ".env.example",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "docs/github_private_repo_setup.md",
    "docs/github_readiness_audit.md",
    "docs/public_release_checklist.md",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/bug_report.md",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def has_sensitive_name(name: str) -> bool:
    """Does this assignment target read as a credential holder?"""
    if name != name.upper():
        return False
    parts = [part for part in name.replace("-", "_").split("_") if part]
    for index, part in enumerate(parts):
        if part not in SENSITIVE_PARTS:
            continue
        # AUTH_RE, TOKEN_PATTERN and friends name a matcher, not a credential.
        if index + 1 < len(parts) and parts[index + 1] in {"RE", "PATTERN", "REGEX", "ENV", "PREFIXES"}:
            continue
        return True
    return False


def looks_like_a_credential(value: str) -> bool:
    """Judge the literal itself, which is the only thing that can be a secret."""
    if any(value.startswith(prefix) for prefix in CREDENTIAL_PREFIXES):
        return True
    if PLACEHOLDER_RE.search(value):
        return False
    if len(value) < MIN_OPAQUE_LENGTH:
        return False
    if "/" in value or value.count("-") > 2 or value.count(".") > 1:
        return False  # a path, a URL, or a readable hyphenated phrase
    has_digit = any(character.isdigit() for character in value)
    has_upper = any(character.isupper() for character in value)
    has_lower = any(character.islower() for character in value)
    if has_digit and has_upper and has_lower:
        return True
    # A long single-case run of hex or base32/64 is also credential-shaped.
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_]{32,}", value))


def sensitive_assignments(text: str, *, strict: bool = False) -> list[str]:
    """Names assigned a literal that looks like a real credential.

    ``strict`` restores the original posture: report every assignment to a
    credential-named variable, whatever the value, for a human to eyeball
    before pushing. That is a reasonable pre-push advisory and a poor gate,
    because it fires on every placeholder and every honest constant, so the
    default judges the value and ``--strict`` is opt-in.
    """
    found = []
    for line in text.splitlines():
        match = ASSIGNMENT_RE.match(line)
        if not match:
            continue
        name, _quote, value = match.groups()
        if has_sensitive_name(name) and (strict or looks_like_a_credential(value)):
            found.append(name)
    return found


def suspicious_files(root: Path, paths: list[str], *, strict: bool = False) -> list[str]:
    suspicious: list[str] = []
    for path in paths:
        if path in ALLOWLISTED_FILES:
            continue
        full_path = root / path
        if not full_path.is_file():
            continue
        try:
            text = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if SENSITIVE_FILENAME_RE.search(path) or sensitive_assignments(text, strict=strict):
            suspicious.append(path)
    return suspicious


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Report every assignment to a credential-named variable, whatever "
            "the value. A thorough pre-push read-through; too noisy for CI."
        ),
    )
    args = parser.parse_args()

    root = repo_root()
    suspicious = suspicious_files(root, tracked_files(root), strict=args.strict)
    if suspicious:
        label = "Credential-named assignments" if args.strict else "Potential credentials"
        print(f"{label} found in shareable files:", file=sys.stderr)
        for path in suspicious:
            print(f"- {path}", file=sys.stderr)
        print("Inspect these files manually before pushing. Values are not printed.", file=sys.stderr)
        return 1
    scope = "credential-named assignments" if args.strict else "credential-shaped values"
    print(f"OK: no {scope} found in tracked/addable text files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
