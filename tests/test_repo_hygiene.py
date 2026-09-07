"""Tests for the credential guard.

This check failed CI on every run for two days because it fired on names
rather than values, flagging `PROBE_TOKEN = "robusto-probe-ok"` and the
gateway constants in the very files that handle authentication. A guard that
cries wolf there is worse than none: it is loudest exactly where a real key
would hide.

So both directions are tested. It must still catch a credential, and it must
stay quiet about the repository's own legitimate constants.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_tracked_sensitive_names import (  # noqa: E402
    has_sensitive_name,
    looks_like_a_credential,
    sensitive_assignments,
)

# Kept out of NAME = "value" form on purpose: written that way, this file would
# trip the very check it tests.
OPAQUE_SAMPLES = [
    "6liZKl4JiiOn3rviGG8fu0Z7s22g",           # the shape of the real Portkey key
    "sk-ant-api03-abcdefghijklmnop",
    "ghp_AbCdEf0123456789AbCdEf0123456789",
    "AKIAIOSFODNN7EXAMPLEX",
    "xoxb-1234567890-abcdefghijklm",
    "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6",
]

INNOCENT_SAMPLES = [
    "robusto-probe-ok",                        # PROBE_TOKEN
    "ROBUSTO_CLAUDE_BIN",                      # an env var name
    "CLAUDE_CODE_EXECPATH",
    "https://api.anthropic.com",
    "work/paper/parsed/full_text.md",
    "your-token-here",
    "changeme",
    "not set",
    "",
    "gateway-key-placeholder",
    "x-portkey-api-key: {token}",              # an f-string template
]


class ValueJudgement(unittest.TestCase):
    def test_credential_shaped_values_are_caught(self) -> None:
        for value in OPAQUE_SAMPLES:
            self.assertTrue(looks_like_a_credential(value), value)

    def test_ordinary_values_are_left_alone(self) -> None:
        for value in INNOCENT_SAMPLES:
            self.assertFalse(looks_like_a_credential(value), value)

    def test_a_short_opaque_value_is_not_reported_on_its_own(self) -> None:
        """Below the length floor, only a known issuer prefix counts."""
        self.assertFalse(looks_like_a_credential("aB3dE6"))
        self.assertTrue(looks_like_a_credential("sk-aB3"))


class NameJudgement(unittest.TestCase):
    def test_credential_holders_are_recognised(self) -> None:
        for name in ("API_KEY", "ANTHROPIC_AUTH_TOKEN", "DB_PASSWORD", "MY_SECRET"):
            self.assertTrue(has_sensitive_name(name), name)

    def test_matchers_and_env_var_names_are_not_holders(self) -> None:
        for name in ("AUTH_RE", "TOKEN_PATTERN", "SECRET_REGEX", "CLAUDE_BIN_ENV",
                     "CREDENTIAL_PREFIXES"):
            self.assertFalse(has_sensitive_name(name), name)

    def test_lowercase_names_are_ignored(self) -> None:
        self.assertFalse(has_sensitive_name("api_key"))


class AssignmentScan(unittest.TestCase):
    def test_a_pasted_key_is_reported(self) -> None:
        text = 'ANTHROPIC_AUTH_TOKEN = "' + OPAQUE_SAMPLES[0] + '"\n'
        self.assertEqual(sensitive_assignments(text), ["ANTHROPIC_AUTH_TOKEN"])

    def test_the_constants_that_broke_ci_are_silent(self) -> None:
        text = (
            'PROBE_TOKEN = "robusto-probe-ok"\n'
            "AUTH_SIGNATURES = (\n"
            '    "oauth access token has expired",\n'
            ")\n"
            'CLAUDE_BIN_ENV = "ROBUSTO_CLAUDE_BIN"\n'
            'EXECPATH_ENV = "CLAUDE_CODE_EXECPATH"\n'
        )
        self.assertEqual(sensitive_assignments(text), [])

    def test_a_non_literal_assignment_is_not_scanned(self) -> None:
        """A tuple, a call or a variable cannot be a pasted secret."""
        text = (
            "API_KEY = os.environ.get('ANTHROPIC_API_KEY')\n"
            "AUTH_TOKEN = read_token()\n"
            "SECRET_PARTS = (1, 2, 3)\n"
        )
        self.assertEqual(sensitive_assignments(text), [])

    def test_indented_and_exported_assignments_are_seen(self) -> None:
        key = OPAQUE_SAMPLES[0]
        self.assertEqual(sensitive_assignments('    MY_TOKEN = "' + key + '"'), ["MY_TOKEN"])
        self.assertEqual(sensitive_assignments('export API_KEY="' + key + '"'), ["API_KEY"])


class LiveRepository(unittest.TestCase):
    def test_the_repository_passes_its_own_guard(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/check_tracked_sensitive_names.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
