"""Seeded-defect calibration against a PDF-shaped parse.

A PDF parse records the same paper in a different shape from a LaTeX one.
Tables live in sidecar files rather than inline, cross-references are the words
on the page rather than macros, and there is no sectioning markup to anchor on.
Every seeder read only the LaTeX shape, so on a `--pdf` run all three found no
target, calibration skipped itself, and the run reported no detection rate at
all rather than reporting a miss. These tests pin the PDF shape down.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from calibrate import (  # noqa: E402
    is_prose_line,
    seed_broken_crossref,
    seed_contradicted_number,
    seed_defects,
    seed_uncited_reference,
    table_body,
)

BS = chr(92)


class PdfShapedParse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parsed = Path(self.tmp.name)
        (self.parsed / "tables").mkdir()
        (self.parsed / "tables" / "table_1.txt").write_text(
            "Effect 0.166 0.201", encoding="utf-8"
        )
        # No "body" key: the PDF path records a path, not the contents.
        (self.parsed / "tables" / "table_inventory.json").write_text(
            json.dumps(
                [
                    {
                        "table_id": 1,
                        "table_label": "1",
                        "source": "caption_text_fallback",
                        "text_path": "work/demo/parsed/tables/table_1.txt",
                    }
                ]
            ),
            encoding="utf-8",
        )
        # No "latex_ref": the PDF path records the words on the page.
        (self.parsed / "crossrefs.json").write_text(
            json.dumps(
                [
                    {
                        "source": "coordinated_text_reference",
                        "kind": "Figure",
                        "label": "5",
                        "reference_text": "Figure 5",
                    },
                    {
                        "source": None,
                        "kind": "Figure",
                        "label": "8",
                        "reference_text": "Figure 8",
                    },
                ]
            ),
            encoding="utf-8",
        )
        (self.parsed / "reference_list.json").write_text(
            json.dumps([{"reference_id": 1, "text": "Doe, J. (2020)."}]),
            encoding="utf-8",
        )

    def rng(self) -> random.Random:
        return random.Random(1)


class TableBodies(PdfShapedParse):
    def test_an_inline_body_is_used_when_present(self) -> None:
        self.assertEqual(table_body(self.parsed, {"body": "Effect & 0.5"}), "Effect & 0.5")

    def test_a_sidecar_body_is_read_when_there_is_no_inline_one(self) -> None:
        [table] = json.loads(
            (self.parsed / "tables" / "table_inventory.json").read_text(encoding="utf-8")
        )
        self.assertIn("0.166", table_body(self.parsed, table))

    def test_a_table_with_neither_yields_nothing_rather_than_failing(self) -> None:
        self.assertEqual(table_body(self.parsed, {"table_id": 9}), "")


class ProseAgainstFlattenedTables(PdfShapedParse):
    def test_a_row_of_figures_is_not_a_claim_about_one(self) -> None:
        self.assertFalse(is_prose_line("Teacher helps me when I ask 0.97 0.96 0.97 0.98"))

    def test_a_sentence_citing_one_number_is_still_prose(self) -> None:
        self.assertTrue(
            is_prose_line("The estimated effect on scores is 0.166 standard deviations.")
        )


class Seeding(PdfShapedParse):
    def test_a_table_body_in_a_sidecar_file_is_still_found(self) -> None:
        text = "The estimated effect on test scores is 0.166 standard deviations here."
        result = seed_contradicted_number(text, self.parsed, self.rng())
        self.assertIsNotNone(result, "the sidecar table body must be read")
        mutated, mutation = result
        self.assertEqual(mutation.original, "0.166")
        self.assertNotIn("is 0.166 standard", mutated)

    def test_a_flattened_table_row_is_not_seeded(self) -> None:
        """PDF parses put table rows in the same file as the prose."""
        text = "Teacher helps me when I ask 0.166 0.201 0.166 0.201"
        self.assertIsNone(seed_contradicted_number(text, self.parsed, self.rng()))

    def test_a_prose_crossref_is_repointed_past_its_own_series(self) -> None:
        text = "As shown in Figure 5, predicted and estimated effects agree closely here."
        result = seed_broken_crossref(text, self.parsed, self.rng())
        self.assertIsNotNone(result, "a text reference must be seedable")
        mutated, mutation = result
        self.assertIn("Figure 48", mutated)
        self.assertNotIn("in Figure 5,", mutated)
        self.assertEqual(mutation.target_auditor, "crossref_auditor")

    def test_a_caption_is_not_mistaken_for_a_reference(self) -> None:
        text = "Figure 5: Predicted versus estimated effects on test scores by arm"
        self.assertIsNone(seed_broken_crossref(text, self.parsed, self.rng()))

    def test_a_label_the_paper_already_uses_is_not_planted(self) -> None:
        """Repointing at something that exists would contradict nothing."""
        text = (
            "As shown in Figure 5, predicted and estimated effects agree closely here. "
            "Figure 48 reports the same comparison for the kindergarten applicants."
        )
        self.assertIsNone(seed_broken_crossref(text, self.parsed, self.rng()))

    def test_a_citation_is_added_in_the_style_of_the_page(self) -> None:
        text = "We study private preschool markets in urban India, where fees vary widely."
        result = seed_uncited_reference(text, self.parsed, self.rng())
        self.assertIsNotNone(result, "an anchor must be found without markup")
        mutated, mutation = result
        self.assertIn("(Seededmissing, 2026)", mutated)
        self.assertNotIn(BS + "citep", mutated)
        self.assertEqual(mutation.token, "Seededmissing")

    def test_all_three_classes_seed_on_a_pdf_shaped_parse(self) -> None:
        body = [
            "We study private preschool markets in urban India, where fees vary widely.",
            "As shown in Figure 5, the estimated effect is 0.166 standard deviations.",
        ]
        (self.parsed / "full_text.md").write_text(
            chr(10).join(body) + chr(10), encoding="utf-8"
        )
        mutations = seed_defects(self.parsed)
        self.assertEqual(
            {mutation.defect_class for mutation in mutations},
            {
                "numeric_contradiction",
                "dangling_cross_reference",
                "citation_missing_from_bibliography",
            },
        )


if __name__ == "__main__":
    unittest.main()
