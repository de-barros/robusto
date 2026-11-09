"""Stage 1 Registered Reports: a protocol reviewed before any results exist."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import refresh_editor  # noqa: E402
import review_paper  # noqa: E402
from reviewer_config import load_reviewers_config, reviewers_for_manuscript  # noqa: E402

CONFIG = REPO_ROOT / "config" / "reviewers.json"
RESULTS_ONLY = {
    "claim_evidence_auditor",
    "abstract_conclusion_consistency_auditor",
    "robustness_auditor",
    "economic_magnitude_auditor",
    "data_availability_replication_auditor",
}
NOTE = "Manuscript type: Stage 1 Registered Report."


class Roster(unittest.TestCase):
    def names(self, kind: str) -> set[str]:
        return {r.name for r in reviewers_for_manuscript(load_reviewers_config(CONFIG), kind)}

    def test_a_paper_keeps_the_results_auditors_and_skips_the_protocol_one(self) -> None:
        names = self.names("paper")
        self.assertTrue(RESULTS_ONLY <= names)
        self.assertNotIn("registered_report_auditor", names)

    def test_a_registered_report_swaps_them(self) -> None:
        names = self.names("registered_report")
        self.assertFalse(RESULTS_ONLY & names)
        self.assertIn("registered_report_auditor", names)
        self.assertIn("power_multiple_testing_auditor", names)
        self.assertIn("devils_advocate_auditor", names)

    def test_the_protocol_auditor_is_mandatory_and_standard_tier(self) -> None:
        auditor = next(r for r in load_reviewers_config(CONFIG) if r.name == "registered_report_auditor")
        self.assertEqual(auditor.selection_policy, "mandatory")
        self.assertEqual(auditor.model_tier, "standard")

    def test_unknown_types_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            reviewers_for_manuscript(load_reviewers_config(CONFIG), "preprint")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewers.json"
            path.write_text(
                json.dumps(
                    {
                        "reviewers": [
                            {
                                "name": "x_auditor",
                                "prompt": "x.txt",
                                "output": "x.json",
                                "id_prefix": "X",
                                "normalization_role": "manuscript",
                                "manuscript_types": ["preprint"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "manuscript_types"):
                load_reviewers_config(path)


class Prompts(unittest.TestCase):
    def render(self, kind: str) -> Path:
        out = Path(tempfile.mkdtemp(prefix="robusto-rr-prompts-"))
        self.addCleanup(lambda: [f.unlink() for f in out.iterdir()] and None or out.rmdir())
        subprocess.run(
            [
                sys.executable,
                "scripts/render_prompts.py",
                "--paper-id", "paper-x",
                "--parsed-dir", "work/paper-x/parsed",
                "--reviews-dir", "work/paper-x/reviews",
                "--schema-path", "schemas/reviewer_output.schema.json",
                "--output-dir", str(out),
                "--manuscript-type", kind,
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        return out

    def test_registered_report_framing_reaches_every_reviewer_and_the_editor(self) -> None:
        out = self.render("registered_report")
        for prompt in out.glob("*_audit.txt"):
            self.assertIn(NOTE, prompt.read_text(encoding="utf-8"), prompt.name)
        editor = (out / "editor_report.txt").read_text(encoding="utf-8")
        self.assertIn("in-principle acceptance", editor)
        self.assertIn("## Appendix: Traceability Map", editor)

    def test_papers_get_no_framing(self) -> None:
        out = self.render("paper")
        for prompt in out.glob("*.txt"):
            self.assertNotIn(NOTE, prompt.read_text(encoding="utf-8"), prompt.name)

    def test_the_selector_is_told_too(self) -> None:
        reviewers = reviewers_for_manuscript(load_reviewers_config(CONFIG), "registered_report")
        optional = [r for r in reviewers if r.selection_policy == "optional"]
        parsed = REPO_ROOT / "work" / "paper-x" / "parsed"
        schema = REPO_ROOT / "schemas" / "reviewer_selection.schema.json"
        with_note = review_paper.render_selector_prompt(
            REPO_ROOT, "paper-x", parsed, optional, schema, "registered_report"
        )
        without = review_paper.render_selector_prompt(REPO_ROOT, "paper-x", parsed, optional, schema)
        self.assertIn(NOTE, with_note)
        self.assertNotIn(NOTE, without)


class Refresh(unittest.TestCase):
    def test_refresh_keeps_the_run_manuscript_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "run_manifest.json"
            self.assertEqual(refresh_editor.run_manuscript_type(manifest), "paper")
            manifest.write_text(json.dumps({"manuscript_type": "registered_report"}), encoding="utf-8")
            self.assertEqual(refresh_editor.run_manuscript_type(manifest), "registered_report")


if __name__ == "__main__":
    unittest.main()
