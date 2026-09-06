"""End-to-end run of the pipeline with the mock backend.

This is the test that proves the machinery rather than its parts: parse, prompt
rendering, preflight gate, applicability routing, twenty reviewers in parallel
batches, the schema contract and its bounded retry, semantic validation,
normalisation, editor-input assembly, editor, final-report check, and resume.
No model call is made and no CLI need be installed, so it runs in CI.

It takes ten to twenty seconds, almost all of it Python start-up for the
subprocesses the pipeline spawns.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tiny_paper" / "main.tex"
PAPER_ID = "test-tmp-pipeline"
WORK = REPO_ROOT / "work" / PAPER_ID
OUTPUTS = REPO_ROOT / "outputs" / PAPER_ID


def run_pipeline(*extra: str, **env_overrides: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBUSTO_MOCK_")}
    env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            "scripts/review_paper.py",
            "--backend", "mock",
            "--source", str(FIXTURE),
            "--paper-id", PAPER_ID,
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
        env=env,
        timeout=600,
    )


class MockPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(OUTPUTS, ignore_errors=True)
        cls.result = run_pipeline()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(OUTPUTS, ignore_errors=True)

    def test_run_completes(self) -> None:
        self.assertEqual(self.result.returncode, 0, self.result.stdout[-3000:] + self.result.stderr[-3000:])
        self.assertIn("[done] report:", self.result.stdout)

    def test_every_stage_reported_ok(self) -> None:
        for stage in ("preprocess", "render-prompts", "reviewer-selector", "normalize",
                      "build-editor-input", "editor", "check-final-report"):
            self.assertIn(f"[ok] {stage}", self.result.stdout, stage)
        self.assertNotIn("[fail]", self.result.stdout)

    def test_the_parse_followed_the_input(self) -> None:
        manifest = json.loads((WORK / "parsed" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "source")
        self.assertIn("results.tex", manifest["settings"]["expanded_files"])
        self.assertEqual(manifest["summary"]["undefined_reference_labels"], [])
        self.assertGreaterEqual(manifest["summary"]["table_count"], 1)
        self.assertIn("source_pdf_sha256", manifest)

    def test_all_twenty_review_stage_auditors_ran(self) -> None:
        run = json.loads((WORK / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["backend"], "mock")
        self.assertEqual(len(run["selected_reviewers"]), 20)
        for name in run["selected_reviewers"]:
            self.assertTrue((WORK / "reviews" / f"{name}.json").is_file(), name)

    def test_preflight_warning_fired_without_blocking(self) -> None:
        self.assertIn("[warn] parser quality:", self.result.stdout)

    def test_report_is_marked_as_mock_and_passes_its_check(self) -> None:
        report = (OUTPUTS / "report.md").read_text(encoding="utf-8")
        self.assertTrue(report.startswith("# Multi-Agent Paper Review Report"))
        self.assertIn("MOCK BACKEND", report)
        self.assertIn("## Appendix: Traceability Map", report)
        self.assertIn("[ok] check-final-report", self.result.stdout)

    def test_resume_after_preflight_reuses_the_parse(self) -> None:
        """Depends on the parse manifest recording the digest resume compares."""
        result = run_pipeline("--resume-after-preflight")
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIn("[resume] reusing deterministic parsed artifacts", result.stdout)
        self.assertNotIn("[run] preprocess", result.stdout)
        self.assertNotIn("[start] parser_quality_auditor", result.stdout)
        run = json.loads((WORK / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(run["resumed_after_preflight"])


class MockPipelineFailure(unittest.TestCase):
    """A reviewer that never meets the contract must stop the run, by name."""

    def setUp(self) -> None:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(OUTPUTS, ignore_errors=True)

    tearDown = setUp

    def test_persistent_drift_fails_loudly_after_one_retry(self) -> None:
        result = run_pipeline(ROBUSTO_MOCK_FAIL="robustness_auditor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[retry] robustness_auditor", result.stdout)
        self.assertIn("[fail] robustness_auditor exited 65", result.stdout)
        self.assertIn("Reviewer run failed: robustness_auditor (65)", result.stderr)
        stderr_log = (WORK / "logs" / "robustness_auditor.stderr.log").read_text(encoding="utf-8")
        self.assertIn("Schema contract not met after one retry", stderr_log)
        self.assertFalse((OUTPUTS / "report.md").exists(), "no report may be written for a failed run")

    def test_transient_drift_is_recovered_by_the_retry(self) -> None:
        result = run_pipeline(ROBUSTO_MOCK_DRIFT="numerical_auditor")
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIn("[retry] numerical_auditor", result.stdout)
        self.assertIn("[ok] numerical_auditor", result.stdout)


class MockPipelineStopAfter(unittest.TestCase):
    """Stopping early is how a first real run is made cheap.

    With the real backend, --stop-after preflight is one model call and proves
    the schema contract on this paper; selection is two. Resume then carries on
    from the parsed artifacts without paying for either again.
    """

    def setUp(self) -> None:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(OUTPUTS, ignore_errors=True)

    tearDown = setUp

    def manifest(self) -> dict:
        return json.loads((WORK / "run_manifest.json").read_text(encoding="utf-8"))

    def test_stop_after_preflight_then_resume_to_completion(self) -> None:
        stopped = run_pipeline("--stop-after", "preflight")
        self.assertEqual(stopped.returncode, 0, stopped.stdout[-2000:] + stopped.stderr[-2000:])
        self.assertIn("[stop] preflight validated", stopped.stdout)
        self.assertIn("[ok] parser_quality_auditor", stopped.stdout)
        self.assertNotIn("reviewer-selector", stopped.stdout)
        self.assertNotIn("[start] crossref_auditor", stopped.stdout)
        self.assertFalse((OUTPUTS / "report.md").exists())
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "stopped")
        self.assertEqual(manifest["stopped_after"], "preflight")

        resumed = run_pipeline("--resume-after-preflight")
        self.assertEqual(resumed.returncode, 0, resumed.stdout[-2000:] + resumed.stderr[-2000:])
        self.assertIn("[resume] reusing deterministic parsed artifacts", resumed.stdout)
        self.assertNotIn("[start] parser_quality_auditor", resumed.stdout)
        self.assertIn("[ok] reviewer-selector", resumed.stdout)
        self.assertIn("[done] report:", resumed.stdout)
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "complete")
        self.assertTrue(manifest["resumed_after_preflight"])
        self.assertTrue((OUTPUTS / "report.md").exists())

    def test_stop_after_selection_renders_prompts_and_names_the_roster(self) -> None:
        stopped = run_pipeline("--stop-after", "selection")
        self.assertEqual(stopped.returncode, 0, stopped.stdout[-2000:] + stopped.stderr[-2000:])
        self.assertIn("[ok] reviewer-selector", stopped.stdout)
        self.assertIn("[selection] applicability roster:", stopped.stdout)
        self.assertIn("[ok] render-selected-prompts", stopped.stdout)
        self.assertIn("[stop] 20 reviewers selected", stopped.stdout)
        self.assertNotIn("[start] crossref_auditor", stopped.stdout)
        self.assertFalse((OUTPUTS / "report.md").exists())
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "stopped")
        self.assertEqual(manifest["stopped_after"], "selection")
        self.assertEqual(len(manifest["selected_reviewers"]), 20)
        self.assertTrue((WORK / "prompts" / "crossref_audit.txt").is_file())


if __name__ == "__main__":
    unittest.main()
