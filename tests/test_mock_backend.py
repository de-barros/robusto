"""Tests for the mock backend.

The mock is driven exactly as the pipeline drives it: as a subprocess reading
a prompt from stdin, with the repo root as the working directory. What matters
is not that it prints something, but that what it prints survives the same
validators a real reply must survive: the JSON schema, the semantic reviewer
rules, and the final-report check.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import claude_backend  # noqa: E402
from check_final_report import report_failures  # noqa: E402
from claude_backend import claude_exec_command, schema_errors  # noqa: E402
from reviewer_config import load_reviewers_config  # noqa: E402
from validate_review_json import semantic_errors  # noqa: E402

MOCK = REPO_ROOT / "scripts" / "mock_claude.py"
PAPER_ID = "test-tmp-mock"
WORK = REPO_ROOT / "work" / PAPER_ID
REVIEWER_SCHEMA = REPO_ROOT / "schemas" / "reviewer_output.schema.json"
SELECTION_SCHEMA = REPO_ROOT / "schemas" / "reviewer_selection.schema.json"


def run_mock(prompt: str, **env_overrides: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBUSTO_MOCK_")}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(MOCK)],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        env=env,
    )


def reviewer_prompt(reviewer: str, prefix: str, retry: bool = False) -> str:
    text = (
        f"Act as the {reviewer} for this run.\n\n"
        f'- reviewer = "{reviewer}"\n- paper_id = "{PAPER_ID}"\n'
        f"- finding IDs must be stable and formatted {prefix}-001, {prefix}-002, ...\n\n"
        "# Output contract\n\nReturn one JSON object.\n"
    )
    if retry:
        text += "\n\nYour previous reply did not satisfy the output contract.\n"
    return text


def selector_prompt(catalog: list[dict]) -> str:
    return (
        "Act as the reviewer_applicability_router for this run.\n\n"
        f'Classify paper_id "{PAPER_ID}" using parsed artifacts.\n\n'
        "```json\n" + json.dumps(catalog) + "\n```\n\n# Output contract\n"
    )


def extract(stdout: str) -> dict:
    from claude_backend import extract_json_object

    text = extract_json_object(stdout)
    assert text is not None, stdout[:200]
    return json.loads(text)


class MockFixture(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(WORK, ignore_errors=True)
        (WORK / "parsed").mkdir(parents=True)
        (WORK / "parsed" / "full_text.md").write_text("# Tiny\n\nSome text.\n", encoding="utf-8")
        (WORK / "parsed" / "manifest.json").write_text(
            json.dumps({"summary": {"page_count": 0}}), encoding="utf-8"
        )
        self.reviewers = load_reviewers_config(REPO_ROOT / "config" / "reviewers.json")

    def tearDown(self) -> None:
        shutil.rmtree(WORK, ignore_errors=True)


class Probe(unittest.TestCase):
    def test_echoes_the_nonce(self) -> None:
        result = run_mock(f"Reply with exactly this: {claude_backend.PROBE_TOKEN}")
        self.assertEqual(result.returncode, 0)
        self.assertIn(claude_backend.PROBE_TOKEN, result.stdout)

    def test_unrecognised_prompt_fails_loudly(self) -> None:
        result = run_mock("Tell me a story.")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognised", result.stderr)


class ReviewerReplies(MockFixture):
    def assert_valid(self, payload: dict, reviewer: str) -> None:
        self.assertEqual(schema_errors(payload, REVIEWER_SCHEMA), [])
        self.assertEqual(semantic_errors(payload, self.reviewers, repo=REPO_ROOT), [])
        self.assertEqual(payload["reviewer"], reviewer)
        self.assertEqual(payload["paper_id"], PAPER_ID)

    def test_a_plain_reviewer_passes_both_validators(self) -> None:
        result = run_mock(reviewer_prompt("robustness_auditor", "ROB"))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = extract(result.stdout)
        self.assert_valid(payload, "robustness_auditor")
        self.assertEqual(len(payload["findings"]), 2)
        ids = [f["id"] for f in payload["findings"]]
        self.assertEqual(ids, ["ROB-001", "ROB-002"])

    def test_numerical_auditor_carries_a_numeric_check(self) -> None:
        """The semantic validator requires it for verifiable numerical findings."""
        payload = extract(run_mock(reviewer_prompt("numerical_auditor", "NUM")).stdout)
        self.assert_valid(payload, "numerical_auditor")
        verifiable = [f for f in payload["findings"] if f["assessment"] != "cannot_verify"]
        self.assertTrue(all(f["numeric_check"] for f in verifiable))

    def test_grammar_auditor_emits_copyedit_findings(self) -> None:
        payload = extract(run_mock(reviewer_prompt("grammar_auditor", "GRAM")).stdout)
        self.assert_valid(payload, "grammar_auditor")
        self.assertIn("copyedit_issue", {f["issue_type"] for f in payload["findings"]})

    def test_parser_auditor_warns_without_blocking(self) -> None:
        """High severity plus high confidence would halt the run; medium only warns."""
        payload = extract(run_mock(reviewer_prompt("parser_quality_auditor", "PARSER")).stdout)
        self.assert_valid(payload, "parser_quality_auditor")
        artifacts = [f for f in payload["findings"] if f["issue_type"] == "parser_artifact"]
        self.assertTrue(artifacts)
        for finding in artifacts:
            self.assertFalse(finding["severity"] == "high" and finding["confidence"] == "high")

    def test_evidence_points_at_a_real_parsed_file(self) -> None:
        payload = extract(run_mock(reviewer_prompt("identification_auditor", "ID")).stdout)
        paths = {
            s["path"] for f in payload["findings"] for s in f["source_objects"]
        }
        self.assertEqual(paths, {f"work/{PAPER_ID}/parsed/full_text.md"})

    def test_findings_count_is_configurable(self) -> None:
        payload = extract(run_mock(reviewer_prompt("robustness_auditor", "ROB"), ROBUSTO_MOCK_FINDINGS="5").stdout)
        self.assertEqual(len(payload["findings"]), 5)
        self.assert_valid(payload, "robustness_auditor")

    def test_every_finding_says_it_is_synthetic(self) -> None:
        payload = extract(run_mock(reviewer_prompt("robustness_auditor", "ROB")).stdout)
        for finding in payload["findings"]:
            self.assertIn("Mock", finding["finding_summary"])
        self.assertIn("mock backend", payload["summary"])


class DriftAndFail(MockFixture):
    def test_drift_breaks_the_first_reply_and_honours_the_retry(self) -> None:
        first = run_mock(reviewer_prompt("robustness_auditor", "ROB"), ROBUSTO_MOCK_DRIFT="robustness_auditor")
        payload = extract(first.stdout)
        self.assertTrue(schema_errors(payload, REVIEWER_SCHEMA), "first reply should fail the schema")

        retry = run_mock(reviewer_prompt("robustness_auditor", "ROB", retry=True), ROBUSTO_MOCK_DRIFT="robustness_auditor")
        payload = extract(retry.stdout)
        self.assertEqual(schema_errors(payload, REVIEWER_SCHEMA), [])

    def test_fail_breaks_every_reply(self) -> None:
        for retry in (False, True):
            result = run_mock(reviewer_prompt("robustness_auditor", "ROB", retry=retry), ROBUSTO_MOCK_FAIL="robustness_auditor")
            payload = extract(result.stdout)
            self.assertTrue(schema_errors(payload, REVIEWER_SCHEMA), f"retry={retry}")

    def test_drift_targets_only_the_named_reviewer(self) -> None:
        result = run_mock(reviewer_prompt("numerical_auditor", "NUM"), ROBUSTO_MOCK_DRIFT="robustness_auditor")
        self.assertEqual(schema_errors(extract(result.stdout), REVIEWER_SCHEMA), [])


class SelectorReplies(MockFixture):
    CATALOG = [
        {"name": "numerical_auditor", "id_prefix": "NUM"},
        {"name": "theory_logic_auditor", "id_prefix": "THEORY"},
        {"name": "robustness_auditor", "id_prefix": "ROB"},
    ]

    def test_selects_everything_by_default(self) -> None:
        payload = extract(run_mock(selector_prompt(self.CATALOG)).stdout)
        self.assertEqual(schema_errors(payload, SELECTION_SCHEMA), [])
        self.assertEqual([s["name"] for s in payload["selected_optional_reviewers"]], [c["name"] for c in self.CATALOG])
        self.assertEqual(payload["skipped_optional_reviewers"], [])
        self.assertEqual(payload["selection_mode"], "applicability")

    def test_skip_knob_moves_reviewers_to_skipped(self) -> None:
        payload = extract(run_mock(selector_prompt(self.CATALOG), ROBUSTO_MOCK_SKIP="theory_logic_auditor").stdout)
        self.assertEqual(schema_errors(payload, SELECTION_SCHEMA), [])
        self.assertEqual([s["name"] for s in payload["skipped_optional_reviewers"]], ["theory_logic_auditor"])
        self.assertNotIn("theory_logic_auditor", [s["name"] for s in payload["selected_optional_reviewers"]])


class EditorReplies(MockFixture):
    def bundle(self, with_copyedit: bool) -> dict:
        findings = [
            {
                "canonical_id": "CANON-001",
                "issue_class": "manuscript_issue",
                "finding_summary": "Synthetic one",
                "source_findings": [
                    {"reviewer": "robustness_auditor", "id": "ROB-001"},
                    {"reviewer": "numerical_auditor", "id": "NUM-001"},
                ],
                "source_objects": [],
            },
            {
                "canonical_id": "CANON-002",
                "issue_class": "cannot_verify",
                "finding_summary": "Synthetic two",
                "source_findings": [{"reviewer": "robustness_auditor", "id": "ROB-002"}],
                "source_objects": [],
            },
        ]
        if with_copyedit:
            findings.append(
                {
                    "canonical_id": "CANON-003",
                    "issue_class": "copyedit_issue",
                    "finding_summary": "Synthetic copyedit",
                    "source_findings": [{"reviewer": "grammar_auditor", "id": "GRAM-001"}],
                    "source_objects": [],
                }
            )
        return {"paper_id": PAPER_ID, "canonical_findings": findings}

    def write_bundle(self, bundle: dict) -> None:
        (WORK / "editor").mkdir(parents=True, exist_ok=True)
        (WORK / "editor" / "normalized_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")

    def editor_prompt(self) -> str:
        return (
            "Act as the editor for this run.\n\n"
            f'Produce the final markdown report for paper_id "{PAPER_ID}".\n'
        )

    def test_report_passes_the_final_report_check_against_its_bundle(self) -> None:
        bundle = self.bundle(with_copyedit=True)
        self.write_bundle(bundle)
        result = run_mock(self.editor_prompt())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report_failures(result.stdout, bundle=bundle), [])

    def test_report_opens_with_the_mock_banner(self) -> None:
        self.write_bundle(self.bundle(with_copyedit=False))
        text = run_mock(self.editor_prompt()).stdout
        self.assertTrue(text.startswith("# Multi-Agent Paper Review Report"))
        self.assertIn("MOCK BACKEND", text.splitlines()[2])
        self.assertIn("not a review", text)

    def test_grammar_appendix_only_when_the_bundle_has_copyedit_findings(self) -> None:
        heading = "## Appendix: Grammar and Copyediting Issues"
        self.write_bundle(self.bundle(with_copyedit=False))
        self.assertNotIn(heading, run_mock(self.editor_prompt()).stdout)
        self.write_bundle(self.bundle(with_copyedit=True))
        self.assertIn(heading, run_mock(self.editor_prompt()).stdout)

    def test_identifiers_appear_only_in_the_traceability_appendix(self) -> None:
        bundle = self.bundle(with_copyedit=True)
        self.write_bundle(bundle)
        text = run_mock(self.editor_prompt()).stdout
        body, _, appendix = text.partition("## Appendix: Traceability Map")
        self.assertNotIn("CANON-", body)
        self.assertNotIn("ROB-001", body)
        for finding in bundle["canonical_findings"]:
            self.assertIn(finding["canonical_id"], appendix)
            for source in finding["source_findings"]:
                self.assertIn(f"{source['reviewer']}:{source['id']}", appendix)

    def test_missing_bundle_fails_loudly(self) -> None:
        result = run_mock(self.editor_prompt())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("normalized_bundle.json", result.stderr)


class BackendSelection(unittest.TestCase):
    def test_default_backend_is_the_real_cli(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBUSTO_BACKEND", None)
            with mock.patch("claude_backend.claude_command", return_value="claude"):
                self.assertEqual(claude_exec_command()[0], "claude")

    def test_mock_backend_substitutes_the_script(self) -> None:
        with mock.patch.dict(os.environ, {"ROBUSTO_BACKEND": "mock"}):
            command = claude_exec_command(model="claude-opus-5", search=True)
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith("mock_claude.py"))
        self.assertEqual(len(command), 2, "the mock takes no model or tool flags")

    def test_mock_backend_needs_no_cli_on_path(self) -> None:
        with mock.patch.dict(os.environ, {"ROBUSTO_BACKEND": "mock"}), mock.patch(
            "claude_backend.claude_command", side_effect=AssertionError("must not be called")
        ):
            claude_exec_command()

    def test_unknown_backend_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"ROBUSTO_BACKEND": "gpt"}):
            with self.assertRaises(claude_backend.BackendUnavailable) as caught:
                claude_exec_command()
        self.assertIn("gpt", str(caught.exception))

    def test_backend_name_is_case_insensitive(self) -> None:
        with mock.patch.dict(os.environ, {"ROBUSTO_BACKEND": "Mock"}):
            self.assertEqual(claude_backend.active_backend(), "mock")


if __name__ == "__main__":
    unittest.main()
