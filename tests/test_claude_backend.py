"""Tests for the Claude Code transport.

The schema contract is the one guarantee this port gives up relative to
upstream's provider-enforced `--output-schema`, so it is the part that needs
tests: recovering JSON from a reply that ignored the contract, and refusing
output that does not validate.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from claude_backend import (  # noqa: E402
    auth_failure_hint,
    claude_exec_command,
    extract_json_object,
    finalize_structured_output,
    retry_prompt,
    schema_errors,
    structured_prompt,
)

SCHEMA = {
    "type": "object",
    "required": ["reviewer", "findings"],
    "properties": {
        "reviewer": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object"}},
    },
}


class ExtractJsonObject(unittest.TestCase):
    def test_plain_object(self) -> None:
        self.assertEqual(extract_json_object('{"a": 1}'), '{"a": 1}')

    def test_fenced_block(self) -> None:
        raw = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
        self.assertEqual(json.loads(extract_json_object(raw)), {"a": 1})

    def test_prose_on_both_sides(self) -> None:
        raw = 'I reviewed it. {"a": 1} Let me know if you need more.'
        self.assertEqual(json.loads(extract_json_object(raw)), {"a": 1})

    def test_nested_objects_are_balanced_correctly(self) -> None:
        raw = 'prefix {"a": {"b": {"c": 1}}, "d": 2} suffix'
        self.assertEqual(json.loads(extract_json_object(raw))["a"]["b"]["c"], 1)

    def test_braces_inside_strings_do_not_confuse_the_scanner(self) -> None:
        raw = '{"note": "a } brace and a { brace", "ok": true}'
        self.assertEqual(json.loads(extract_json_object(raw))["ok"], True)

    def test_escaped_quote_inside_string(self) -> None:
        raw = '{"note": "she said \\"stop\\" firmly", "ok": true}'
        self.assertEqual(json.loads(extract_json_object(raw))["ok"], True)

    def test_returns_none_when_there_is_no_object(self) -> None:
        self.assertIsNone(extract_json_object("I could not complete this review."))
        self.assertIsNone(extract_json_object(""))

    def test_skips_an_unparseable_first_candidate(self) -> None:
        raw = 'noise {not json at all} then {"a": 1}'
        self.assertEqual(json.loads(extract_json_object(raw)), {"a": 1})


class SchemaHandling(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.schema_path = self.dir / "schema.json"
        self.schema_path.write_text(json.dumps(SCHEMA), encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_structured_prompt_carries_the_schema(self) -> None:
        prompt = structured_prompt("Audit the numbers.", self.schema_path)
        self.assertIn("Audit the numbers.", prompt)
        self.assertIn("Output contract", prompt)
        self.assertIn('"findings"', prompt)

    def test_retry_prompt_quotes_the_failures(self) -> None:
        prompt = retry_prompt("Audit.", self.schema_path, ["reviewer: is a required property"])
        self.assertIn("did not satisfy the output contract", prompt)
        self.assertIn("reviewer: is a required property", prompt)

    def test_schema_errors_reports_the_missing_field(self) -> None:
        errors = schema_errors({"findings": []}, self.schema_path)
        self.assertTrue(any("reviewer" in e for e in errors))

    def test_finalize_writes_valid_output(self) -> None:
        raw = self.dir / "out.raw.txt"
        raw.write_text('```json\n{"reviewer": "numerical", "findings": []}\n```', encoding="utf-8")
        target = self.dir / "out.json"
        ok, errors = finalize_structured_output(raw, target, self.schema_path)
        self.assertTrue(ok, errors)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["reviewer"], "numerical")

    def test_finalize_rejects_output_that_fails_the_schema(self) -> None:
        raw = self.dir / "out.raw.txt"
        raw.write_text('{"findings": []}', encoding="utf-8")
        target = self.dir / "out.json"
        ok, errors = finalize_structured_output(raw, target, self.schema_path)
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertFalse(target.exists(), "invalid output must not be written")

    def test_finalize_reports_a_reply_with_no_json(self) -> None:
        raw = self.dir / "out.raw.txt"
        raw.write_text("I was unable to parse the manuscript.", encoding="utf-8")
        ok, errors = finalize_structured_output(raw, self.dir / "out.json", self.schema_path)
        self.assertFalse(ok)
        self.assertIn("no parseable JSON", errors[0])

    def test_finalize_reports_a_missing_capture(self) -> None:
        ok, errors = finalize_structured_output(
            self.dir / "absent.txt", self.dir / "out.json", self.schema_path
        )
        self.assertFalse(ok)
        self.assertIn("no output captured", errors[0])


class AuthDiagnosis(unittest.TestCase):
    """The failure that wasted the first real run: installed but not logged in."""

    def test_expired_oauth_token_is_recognised(self) -> None:
        raw = ("Failed to authenticate. API Error: 401 OAuth access token has "
               "expired. Re-authenticate to continue.")
        hint = auth_failure_hint(raw)
        self.assertIsNotNone(hint)
        self.assertIn("not authenticated", hint)
        self.assertIn("claude", hint)

    def test_other_auth_phrasings_are_recognised(self) -> None:
        for raw in ("Invalid API key provided", "authentication_error", "Please run /login"):
            self.assertIsNotNone(auth_failure_hint(raw), raw)

    def test_ordinary_output_is_not_mistaken_for_an_auth_failure(self) -> None:
        self.assertIsNone(auth_failure_hint('{"reviewer": "numerical", "findings": []}'))
        self.assertIsNone(auth_failure_hint("The paper reports 401 students in the sample."))
        self.assertIsNone(auth_failure_hint(""))
        self.assertIsNone(auth_failure_hint(None))


class CommandConstruction(unittest.TestCase):
    def test_read_only_tools_by_default(self) -> None:
        with mock.patch("claude_backend.claude_command", return_value="claude"):
            self.assertEqual(
                claude_exec_command(), ["claude", "-p", "--allowed-tools", "Read,Grep,Glob"]
            )

    def test_search_adds_the_web_tool(self) -> None:
        with mock.patch("claude_backend.claude_command", return_value="claude"):
            self.assertIn("WebSearch", claude_exec_command(search=True)[-1])

    def test_no_write_tools_are_ever_granted(self) -> None:
        """Reviewers read parsed artifacts; they must not edit the repository."""
        with mock.patch("claude_backend.claude_command", return_value="claude"):
            granted = claude_exec_command(search=True)[-1]
        for forbidden in ("Write", "Edit", "Bash", "NotebookEdit"):
            self.assertNotIn(forbidden, granted)


if __name__ == "__main__":
    unittest.main()
