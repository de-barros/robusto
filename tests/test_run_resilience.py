"""Failures seen on a full twenty-auditor run, each pinned so it cannot return.

1. A semantic slip in one reviewer (precision=missing with a section given)
   passed the schema retry and stopped the whole run after every auditor.
2. A reviewer that read nothing returned schema-valid all-cannot_verify JSON.
3. One failing reviewer aborted the run instead of being dropped.
4. The editor bundle repeated every evidence summary and outgrew its budget.
5. refresh_editor.py --run-editor passed a Codex-only CLI flag.
6. A report failing check_final_report.py on one stray identifier was fatal.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import refresh_editor  # noqa: E402
import review_paper  # noqa: E402
from build_editor_input import (  # noqa: E402
    PRETTY_EDITOR_INPUT_BYTES,
    bounded_editor_input,
    editor_input_document,
)
from normalize_review_outputs import normalize  # noqa: E402
from reviewer_config import ReviewerConfig  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from test_reviewer_config import finding, review_output, reviewer_config  # noqa: E402

SCHEMA = REPO_ROOT / "schemas" / "reviewer_output.schema.json"
SOURCE_PATH = "work/paper-x/parsed/tables/table_1.md"


def grounded_finding(**overrides: object) -> dict[str, object]:
    base = finding()
    base["source_objects"][0]["path"] = SOURCE_PATH  # type: ignore[index]
    base.update(overrides)
    return base


def blind_finding() -> dict[str, object]:
    return finding(
        id="NUM-001",
        category="cannot_verify",
        issue_type="cannot_verify",
        assessment="cannot_verify",
        cannot_verify_reason="No parsed manuscript artifact could be read in this run.",
        source_objects=[],
        claim_evidence_links=[],
        numeric_check=None,
        location={
            "page": None,
            "page_label": None,
            "section": None,
            "text_quote": None,
            "precision": "missing",
        },
    )


class SandboxMixin:
    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="robusto-resilience-"))
        self.addCleanup(shutil.rmtree, root, True)  # type: ignore[attr-defined]
        source = root / SOURCE_PATH
        source.parent.mkdir(parents=True)
        source.write_text("| Table 1 | 12% |\n", encoding="utf-8")
        return root

    def ctx_for(self, root: Path, payload: object) -> dict:
        raw = root / "reviews" / "numerical_auditor.json.raw.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps(payload), encoding="utf-8")
        return {
            "raw_path": raw,
            "output_path": root / "reviews" / "numerical_auditor.json",
            "schema_path": SCHEMA,
            "cwd": root,
        }


class ReviewerRetryCatchesSemanticErrors(SandboxMixin, unittest.TestCase):
    def test_valid_reply_has_no_errors(self) -> None:
        root = self.make_repo()
        ctx = self.ctx_for(root, review_output([grounded_finding()]))
        self.assertEqual(review_paper.structured_reviewer_errors(reviewer_config(), ctx), [])

    def test_precision_missing_with_section_is_caught_before_the_panel_ends(self) -> None:
        root = self.make_repo()
        bad = grounded_finding(
            location={
                "page": None,
                "page_label": None,
                "section": "Appendix 1",
                "text_quote": None,
                "precision": "missing",
            }
        )
        ctx = self.ctx_for(root, review_output([bad]))
        errors = review_paper.structured_reviewer_errors(reviewer_config(), ctx)
        self.assertTrue(
            any("precision=missing but still includes concrete location fields" in e for e in errors),
            errors,
        )

    def test_schema_errors_still_come_first(self) -> None:
        root = self.make_repo()
        ctx = self.ctx_for(root, {"reviewer": "numerical_auditor"})
        errors = review_paper.structured_reviewer_errors(reviewer_config(), ctx)
        self.assertTrue(errors)


class BlindRunsAreRetried(SandboxMixin, unittest.TestCase):
    def write(self, root: Path, payload: object) -> Path:
        path = root / "review.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_all_cannot_verify_with_no_sources_is_blind(self) -> None:
        root = self.make_repo()
        path = self.write(root, review_output([blind_finding()], run_status="cannot_verify"))
        self.assertTrue(review_paper.blind_run_errors(path))

    def test_a_grounded_finding_is_not_blind(self) -> None:
        root = self.make_repo()
        path = self.write(root, review_output([blind_finding(), grounded_finding(id="NUM-002")]))
        self.assertEqual(review_paper.blind_run_errors(path), [])

    def test_no_findings_is_not_blind(self) -> None:
        root = self.make_repo()
        path = self.write(root, review_output([]))
        self.assertEqual(review_paper.blind_run_errors(path), [])


class FailedReviewersDoNotStopTheBatch(unittest.TestCase):
    def test_failures_are_collected_and_later_batches_still_run(self) -> None:
        reviewers = [reviewer_config(f"r{i}", f"R{i}") for i in range(3)]
        started: list[str] = []

        def fake_start(reviewer, *args, **kwargs):
            started.append(reviewer.name)
            return (reviewer,)

        def fake_wait(reviewer, timeout_seconds=None):
            code = 65 if reviewer.name == "r0" else 0
            return review_paper.RunResult(reviewer.name, code, Path("o"), Path("e"))

        failed: list[str] = []
        with mock.patch.object(review_paper, "start_reviewer", fake_start), mock.patch.object(
            review_paper, "wait_reviewer", fake_wait
        ):
            review_paper.run_reviewer_batch(
                reviewers, REPO_ROOT, Path("p"), Path("r"), SCHEMA, Path("l"),
                max_parallel=1, failed=failed,
            )
            self.assertEqual(started, ["r0", "r1", "r2"])
            self.assertEqual(failed, ["r0"])

            with self.assertRaisesRegex(RuntimeError, "Reviewer run failed"):
                review_paper.run_reviewer_batch(
                    reviewers, REPO_ROOT, Path("p"), Path("r"), SCHEMA, Path("l"), max_parallel=1
                )


class RefreshDropsInvalidReviewers(unittest.TestCase):
    def roster(self) -> list[ReviewerConfig]:
        return [
            reviewer_config("numerical_auditor", "NUM"),
            reviewer_config("claim_evidence_auditor", "CEA"),
        ]

    def test_invalid_reviewer_is_dropped_and_the_rest_kept(self) -> None:
        roster = self.roster()

        def fake_run(command, **kwargs):
            invalid = "claim_evidence_auditor" in command[-1]
            return mock.Mock(
                returncode=1 if invalid else 0,
                stdout="INVALID\n- findings.0.location has precision=missing\n" if invalid else "VALID\n",
            )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reviews = repo / "reviews"
            reviews.mkdir()
            for reviewer in roster:
                (reviews / reviewer.output).write_text("{}", encoding="utf-8")
            with mock.patch.object(refresh_editor.subprocess, "run", fake_run):
                kept, dropped = refresh_editor.validated_editor_roster(
                    roster, reviews, repo, strict=False
                )
                self.assertEqual([r.name for r in kept], ["numerical_auditor"])
                self.assertIn("claim_evidence_auditor", dropped)
                self.assertIn("precision=missing", dropped["claim_evidence_auditor"])
                with self.assertRaises(RuntimeError):
                    refresh_editor.validated_editor_roster(roster, reviews, repo, strict=True)

    def test_missing_output_is_dropped(self) -> None:
        roster = self.roster()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reviews = repo / "reviews"
            reviews.mkdir()
            (reviews / roster[0].output).write_text("{}", encoding="utf-8")
            ok = mock.Mock(returncode=0, stdout="VALID\n")
            with mock.patch.object(refresh_editor.subprocess, "run", return_value=ok):
                kept, dropped = refresh_editor.validated_editor_roster(
                    roster, reviews, repo, strict=False
                )
            self.assertEqual([r.name for r in kept], ["numerical_auditor"])
            self.assertIn("missing output", dropped["claim_evidence_auditor"])


class EditorBundleIsLean(unittest.TestCase):
    def test_evidence_and_reasons_are_stored_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reviews = Path(tmp)
            reviewer = reviewer_config()
            (reviews / reviewer.output).write_text(
                json.dumps(review_output([finding()])), encoding="utf-8"
            )
            bundle = normalize("paper-x", reviews, [reviewer])
        group = bundle["canonical_findings"][0]
        detail = group["source_finding_details"][0]
        self.assertNotIn("evidence_summary", detail)
        self.assertNotIn("cannot_verify_reason", detail)
        self.assertEqual(group["evidence_summaries"][0]["text"], "The table reports 12%.")

    def test_large_bundles_are_minified_even_under_the_hard_cap(self) -> None:
        args = {
            "paper_id": "paper-x",
            "editor_prompt_text": "prompt",
            "bundle_path": Path("bundle.json"),
            "bundle_json": {
                "paper_id": "paper-x",
                "canonical_findings": [],
                "padding": [{"k": "v" * 50} for _ in range(PRETTY_EDITOR_INPUT_BYTES // 40)],
            },
            "reviews_dir": Path("reviews"),
            "reviewers": [],
            "review_paths": [],
            "review_json_by_name": {},
            "selection_json": None,
        }
        pretty = len(editor_input_document(**args, compact_bundle=False).encode("utf-8"))
        self.assertGreater(pretty, PRETTY_EDITOR_INPUT_BYTES)
        _, serialization, size = bounded_editor_input(args, max_bytes=pretty * 2)
        self.assertEqual(serialization, "minified")
        self.assertLess(size, pretty)


class EditorPassUsesStdoutAndRepairsOnce(unittest.TestCase):
    def test_refresh_editor_no_longer_passes_codex_flags(self) -> None:
        source = (SCRIPTS_DIR / "refresh_editor.py").read_text(encoding="utf-8")
        self.assertNotIn('"--output-last-message"', source)
        self.assertIn("run_editor_pass(", source)

    def run_pass(self, checks: list[list[str]]) -> tuple[list[str], Path]:
        calls: list[str] = []
        tmp = Path(tempfile.mkdtemp(prefix="robusto-editor-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        report = tmp / "report.md"
        bundle = tmp / "normalized_bundle.json"
        bundle.write_text(
            json.dumps(
                {
                    "canonical_findings": [
                        {"canonical_id": "CANON-001", "source_findings": [{"id": "NUM-001"}]}
                    ]
                }
            ),
            encoding="utf-8",
        )
        editor_input = tmp / "editor_input.md"
        editor_input.write_text("input", encoding="utf-8")

        def fake_run_required(label, command, cwd, log_dir, input_text=None, capture_text_to=None, **kw):
            calls.append(label)
            if label == "editor-repair":
                self.assertIn("stray identifier", input_text)
                self.assertIn("CANON-001: NUM-001", input_text)
            capture_text_to.write_text(f"report from {label}", encoding="utf-8")
            return review_paper.RunResult(label, 0, tmp / f"{label}.out", tmp / f"{label}.err")

        results = iter(checks)
        with mock.patch.object(review_paper, "run_required", fake_run_required), mock.patch.object(
            review_paper, "recover_editor_report_if_needed", lambda *a: None
        ), mock.patch.object(review_paper, "plausible_editor_report", lambda text: True), mock.patch.object(
            review_paper, "final_report_failures", lambda *a: next(results)
        ):
            review_paper.run_editor_pass(
                repo=tmp, log_dir=tmp, editor_input_path=editor_input, report_path=report,
                bundle_path=bundle, model=None, reasoning_effort=None, timeout_seconds=None,
            )
        return calls, report

    def test_valid_report_needs_no_repair(self) -> None:
        calls, _ = self.run_pass([[]])
        self.assertEqual(calls, ["editor"])

    def test_one_failure_triggers_one_repair(self) -> None:
        calls, report = self.run_pass([["stray identifier in body"], []])
        self.assertEqual(calls, ["editor", "editor-repair"])
        self.assertEqual(report.read_text(encoding="utf-8"), "report from editor-repair")
        self.assertTrue((report.parent / "report.rejected.md").exists())

    def test_second_failure_raises_and_restores_the_draft(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "after one repair"):
            self.run_pass([["stray identifier in body"], ["stray identifier in body"]])


if __name__ == "__main__":
    unittest.main()
