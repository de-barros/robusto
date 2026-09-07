"""Tests for the Claude Code transport.

The schema contract is the one guarantee this port gives up relative to
upstream's provider-enforced `--output-schema`, so it is the part that needs
tests: recovering JSON from a reply that ignored the contract, and refusing
output that does not validate.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claude_backend  # noqa: E402
from claude_backend import (  # noqa: E402
    BackendUnavailable,
    auth_failure_hint,
    billing_route_warning,
    probe_authentication,
    claude_command,
    cli_auth_status,
    cli_login_hint,
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


class CliResolution(unittest.TestCase):
    """Finding the CLI.

    PATH is not a proxy for installation: the native Windows installer writes
    the binary to ~/.local/bin and leaves the persisted user PATH alone, which
    made a working install look absent.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.installed = self.dir / "claude.exe"
        self.installed.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Both are set for real when these tests run inside the Claude desktop
        # app, which would otherwise satisfy the resolver before PATH is reached.
        os.environ.pop("ROBUSTO_CLAUDE_BIN", None)
        os.environ.pop("CLAUDE_CODE_EXECPATH", None)

    def test_path_is_used_when_it_carries_the_cli(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/claude"):
            self.assertEqual(claude_command(), "/usr/bin/claude")

    def test_falls_back_to_a_known_install_directory(self) -> None:
        with mock.patch("shutil.which", return_value=None), mock.patch.object(
            claude_backend, "CLAUDE_FALLBACK_DIRS", (str(self.dir),)
        ):
            self.assertEqual(claude_command(), str(self.installed))

    def test_override_wins_over_path(self) -> None:
        os.environ["ROBUSTO_CLAUDE_BIN"] = str(self.installed)
        with mock.patch("shutil.which", return_value="/usr/bin/claude"):
            self.assertEqual(claude_command(), str(self.installed))

    def test_override_pointing_nowhere_says_so(self) -> None:
        os.environ["ROBUSTO_CLAUDE_BIN"] = str(self.dir / "absent.exe")
        with self.assertRaises(BackendUnavailable) as caught:
            claude_command()
        self.assertIn("ROBUSTO_CLAUDE_BIN", str(caught.exception))

    def test_absent_everywhere_gives_actionable_advice(self) -> None:
        with mock.patch("shutil.which", return_value=None), mock.patch.object(
            claude_backend, "CLAUDE_FALLBACK_DIRS", (str(self.dir / "empty"),)
        ):
            with self.assertRaises(BackendUnavailable) as caught:
                claude_command()
        message = str(caught.exception)
        self.assertIn("ROBUSTO_CLAUDE_BIN", message)
        self.assertIn("claude.com/claude-code", message)


class BillingRoute(unittest.TestCase):
    """Which account pays for the panel.

    Reviewers are separate `claude -p` processes that inherit this environment,
    so a gateway set up for interactive work silently captures a twenty-call
    run as well.
    """

    def _env(self, **overrides):
        base = {name: "" for name in claude_backend.GATEWAY_ENV_VARS}
        base.update(overrides)
        return mock.patch.dict(os.environ, base, clear=False)

    def test_clean_environment_is_silent(self) -> None:
        with self._env():
            for name in claude_backend.GATEWAY_ENV_VARS:
                os.environ.pop(name, None)
            self.assertIsNone(billing_route_warning())

    def test_a_gateway_base_url_is_named(self) -> None:
        with self._env(ANTHROPIC_BASE_URL="https://api.portkey.ai"):
            warning = billing_route_warning()
        self.assertIsNotNone(warning)
        self.assertIn("ANTHROPIC_BASE_URL", warning)
        self.assertIn("subscription", warning)

    def test_every_variable_present_is_listed(self) -> None:
        with self._env(
            ANTHROPIC_BASE_URL="https://api.portkey.ai",
            ANTHROPIC_AUTH_TOKEN="secret",
            CLAUDE_CODE_USE_BEDROCK="1",
        ):
            warning = billing_route_warning()
        for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
            self.assertIn(name, warning)

    def test_the_token_value_is_never_echoed(self) -> None:
        with self._env(ANTHROPIC_AUTH_TOKEN="gateway-key-placeholder"):
            warning = billing_route_warning()
        self.assertNotIn("gateway-key-placeholder", warning)

    def test_empty_values_do_not_count_as_set(self) -> None:
        with self._env(ANTHROPIC_BASE_URL=""):
            self.assertIsNone(billing_route_warning())

    def test_the_default_endpoint_is_not_a_diversion(self) -> None:
        """Presence is not diversion, and a check that cries wolf gets ignored."""
        for value in (
            "https://api.anthropic.com",
            "https://api.anthropic.com/",
            "HTTPS://API.ANTHROPIC.COM",
            "  https://api.anthropic.com  ",
        ):
            with self._env(ANTHROPIC_BASE_URL=value):
                self.assertIsNone(billing_route_warning(), value)

    def test_a_non_default_endpoint_still_warns(self) -> None:
        with self._env(ANTHROPIC_BASE_URL="https://api.portkey.ai"):
            self.assertIsNotNone(billing_route_warning())

    def test_provider_switches_read_as_flags(self) -> None:
        for off in ("0", "false", "no", "off", "FALSE"):
            with self._env(CLAUDE_CODE_USE_BEDROCK=off):
                self.assertIsNone(billing_route_warning(), off)
        for on in ("1", "true", "yes"):
            with self._env(CLAUDE_CODE_USE_BEDROCK=on):
                self.assertIsNotNone(billing_route_warning(), on)

    def test_a_credential_counts_even_at_the_default_endpoint(self) -> None:
        """A token replaces the subscription login wherever it points."""
        with self._env(
            ANTHROPIC_BASE_URL="https://api.anthropic.com",
            ANTHROPIC_AUTH_TOKEN="some-gateway-key",
        ):
            warning = billing_route_warning()
        self.assertIsNotNone(warning)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", warning)
        self.assertNotIn("ANTHROPIC_BASE_URL", warning)


class AuthProbe(unittest.TestCase):
    """The probe must not depend on exit status or on refusal wording.

    An unauthenticated `claude -p` exits 0 and prints its refusal to stdout. A
    blocklist catches today's phrasing; if that phrasing ever changes, the
    refusal flows downstream as reviewer output and surfaces as a puzzling
    schema failure instead of an auth error.
    """

    def _run(self, stdout="", stderr="", returncode=0):
        completed = mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)
        with mock.patch("claude_backend.claude_command", return_value="claude"),              mock.patch("subprocess.run", return_value=completed):
            return probe_authentication()

    def test_a_healthy_reply_passes(self) -> None:
        self.assertIsNone(self._run(stdout=claude_backend.PROBE_TOKEN))

    def test_reply_with_surrounding_prose_still_passes(self) -> None:
        self.assertIsNone(self._run(stdout=f"Sure.\n{claude_backend.PROBE_TOKEN}\n"))

    def test_known_refusal_wording_is_named(self) -> None:
        hint = self._run(stdout="Not logged in - Please run /login", returncode=0)
        self.assertIsNotNone(hint)
        self.assertIn("not authenticated", hint)

    def test_novel_refusal_wording_at_exit_zero_is_still_caught(self) -> None:
        """The case the blocklist alone would miss."""
        hint = self._run(stdout="Your session could not be established.", returncode=0)
        self.assertIsNotNone(hint)
        self.assertIn("did not answer the probe", hint)

    def test_silence_at_exit_zero_is_caught(self) -> None:
        hint = self._run(stdout="", returncode=0)
        self.assertIsNotNone(hint)
        self.assertIn("(nothing)", hint)

    def test_a_nonzero_exit_reports_the_code(self) -> None:
        hint = self._run(stdout="something broke", returncode=3)
        self.assertIn("exited 3", hint)

    def test_the_nonce_cannot_be_satisfied_by_ordinary_prose(self) -> None:
        hint = self._run(stdout="I reviewed the paper and found three problems.")
        self.assertIsNotNone(hint)


class CliLoginState(unittest.TestCase):
    """The blocker that cost two evenings.

    The desktop app and the CLI keep separate credentials. A CLI that was never
    signed in reports "OAuth session expired and could not be refreshed" and
    exits 0, which reads as a lapsed session rather than an absent one. Asking
    `claude auth status` costs nothing and answers it outright.
    """

    def _status(self, payload, returncode=0):
        completed = mock.Mock(stdout=json.dumps(payload), stderr="", returncode=returncode)
        return mock.patch("subprocess.run", return_value=completed)

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
            os.environ.pop(name, None)

    def test_status_is_parsed(self) -> None:
        with mock.patch("claude_backend.claude_command", return_value="claude"), self._status(
            {"loggedIn": True, "authMethod": "claudeai"}
        ):
            self.assertEqual(cli_auth_status()["authMethod"], "claudeai")

    def test_a_signed_in_cli_produces_no_hint(self) -> None:
        self.assertIsNone(cli_login_hint({"loggedIn": True, "authMethod": "claudeai"}))

    def test_a_signed_out_cli_names_the_exact_command(self) -> None:
        hint = cli_login_hint({"loggedIn": False, "authMethod": "none"})
        self.assertIsNotNone(hint)
        self.assertIn("claude auth login --claudeai", hint)

    def test_under_the_desktop_app_it_explains_why_being_logged_in_is_not_enough(self) -> None:
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        hint = cli_login_hint({"loggedIn": False})
        self.assertIn("does not sign in the CLI", hint)
        self.assertIn("same subscription", hint)

    def test_outside_the_desktop_app_it_stays_short(self) -> None:
        hint = cli_login_hint({"loggedIn": False})
        self.assertNotIn("desktop app", hint)

    def test_unreadable_status_yields_no_hint(self) -> None:
        """Never block a run on a status command that could not be read."""
        with mock.patch("claude_backend.claude_command", return_value="claude"), mock.patch(
            "subprocess.run", side_effect=OSError("boom")
        ):
            self.assertIsNone(cli_auth_status())
            self.assertIsNone(cli_login_hint())

    def test_the_probe_reports_the_login_without_spending_a_call(self) -> None:
        calls = []

        def record(command, **kwargs):
            calls.append(command)
            return mock.Mock(
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
                returncode=0,
            )

        with mock.patch("claude_backend.claude_command", return_value="claude"), mock.patch(
            "subprocess.run", side_effect=record
        ):
            hint = probe_authentication()
        self.assertIn("claude auth login", hint)
        self.assertEqual(len(calls), 1, "only the free status call should run")
        self.assertIn("auth", calls[0])


class ExecPathResolution(unittest.TestCase):
    """The desktop app names the binary it runs; prefer it to any guess."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.binary = Path(self.tmp.name) / "claude.exe"
        self.binary.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ROBUSTO_CLAUDE_BIN", None)
        os.environ.pop("CLAUDE_CODE_EXECPATH", None)

    def test_execpath_wins_over_path(self) -> None:
        os.environ["CLAUDE_CODE_EXECPATH"] = str(self.binary)
        with mock.patch("shutil.which", return_value="/usr/bin/claude"):
            self.assertEqual(claude_command(), str(self.binary))

    def test_an_explicit_override_still_wins_over_execpath(self) -> None:
        os.environ["CLAUDE_CODE_EXECPATH"] = str(self.binary)
        other = Path(self.tmp.name) / "other.exe"
        other.write_text("", encoding="utf-8")
        os.environ["ROBUSTO_CLAUDE_BIN"] = str(other)
        self.assertEqual(claude_command(), str(other))

    def test_a_stale_execpath_falls_through_to_path(self) -> None:
        os.environ["CLAUDE_CODE_EXECPATH"] = str(Path(self.tmp.name) / "gone.exe")
        with mock.patch("shutil.which", return_value="/usr/bin/claude"):
            self.assertEqual(claude_command(), "/usr/bin/claude")


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
