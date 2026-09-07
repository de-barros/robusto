"""Tests for seeded-defect calibration.

The measurement is only worth having if the defects it plants are real. Two
bugs in the first draft made them not: one edited a value inside a table row,
and one edited a macro definition whose uses the parse had already expanded.
Both produced a "defect" that contradicted nothing, which would then be scored
as a miss and blamed on the panel. Most of what follows guards that boundary.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from calibrate import (  # noqa: E402
    Mutation,
    is_prose_line,
    line_at,
    match_findings,
    render_report,
    seed_broken_crossref,
    seed_contradicted_number,
    seed_defects,
    seed_uncited_reference,
    shift_number,
    tabular_spans,
    unmatched_finding_count,
)

BS = chr(92)


class NumberShifting(unittest.TestCase):
    def test_the_value_changes(self) -> None:
        for value in ("0.166", "12.02", "8", "1,240"):
            self.assertNotEqual(shift_number(value), value, value)

    def test_the_shape_is_preserved(self) -> None:
        self.assertEqual(len(shift_number("0.166")), len("0.166"))
        self.assertTrue(shift_number("12.02").startswith("12.0"))

    def test_a_value_with_no_digits_is_returned_unchanged(self) -> None:
        self.assertEqual(shift_number("n.a."), "n.a.")


class ProseDetection(unittest.TestCase):
    """Deciding what counts as a claim, rather than markup that looks like one."""

    def test_line_at_returns_the_whole_line(self) -> None:
        self.assertEqual(line_at("alpha\nbeta gamma\ndelta", 8), "beta gamma")

    def test_a_macro_definition_is_not_prose(self) -> None:
        self.assertFalse(is_prose_line(BS + "newcommand{" + BS + "effect}{0.15}"))
        self.assertFalse(is_prose_line(BS + "providecommand{" + BS + "x}{[run master.do]}"))

    def test_a_table_row_is_not_prose(self) -> None:
        self.assertFalse(is_prose_line("&&  &  & [11.89] & [12.02] &&  & (0.79) " + BS * 2))

    def test_a_sentence_is_prose(self) -> None:
        self.assertTrue(is_prose_line("The estimated effect is 0.15 standard deviations in mathematics."))

    def test_a_sentence_carrying_markup_is_still_prose(self) -> None:
        self.assertTrue(is_prose_line("Table~" + BS + "ref{tab:main} reports an effect of 0.15."))


class TabularSpans(unittest.TestCase):
    def test_a_table_body_is_covered(self) -> None:
        text = "before\n" + BS + "begin{tabular}{cc}\na & b " + BS * 2 + "\n" + BS + "end{tabular}\nafter"
        spans = tabular_spans(text)
        self.assertEqual(len(spans), 1)
        start, end = spans[0]
        self.assertIn("a & b", text[start:end])
        self.assertNotIn("before", text[start:end])

    def test_longtable_counts_too(self) -> None:
        text = BS + "begin{longtable}{cc}\n1.23 & 4.56 " + BS * 2 + "\n" + BS + "end{longtable}"
        self.assertEqual(len(tabular_spans(text)), 1)


class Seeders(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parsed = Path(self.tmp.name)
        (self.parsed / "tables").mkdir()
        (self.parsed / "tables" / "table_inventory.json").write_text(
            json.dumps([{"body": "Effect & 0.166 " + BS * 2}]), encoding="utf-8"
        )
        (self.parsed / "crossrefs.json").write_text(
            json.dumps([{"source": "latex_ref", "label": "tab:main"}]), encoding="utf-8"
        )
        (self.parsed / "reference_list.json").write_text(
            json.dumps([{"citation_key": "doe2020"}]), encoding="utf-8"
        )

    def rng(self):
        import random
        return random.Random(1)

    def test_the_prose_occurrence_is_the_one_that_moves(self) -> None:
        text = (
            "The estimated effect on test scores is 0.166 standard deviations here.\n"
            + BS + "begin{tabular}{cc}\nEffect & 0.166 " + BS * 2 + "\n" + BS + "end{tabular}\n"
        )
        result = seed_contradicted_number(text, self.parsed, self.rng())
        self.assertIsNotNone(result)
        mutated, mutation = result
        self.assertIn("Effect & 0.166", mutated, "the table must keep the original value")
        self.assertNotIn("is 0.166 standard", mutated, "the prose claim must have moved")
        self.assertEqual(mutation.original, "0.166")
        self.assertEqual(mutation.target_auditor, "numerical_auditor")

    def test_a_value_only_inside_a_table_is_not_seeded(self) -> None:
        """Editing it would contradict nothing, so it must not be chosen."""
        text = BS + "begin{tabular}{cc}\nEffect & 0.166 " + BS * 2 + "\n" + BS + "end{tabular}\n"
        self.assertIsNone(seed_contradicted_number(text, self.parsed, self.rng()))

    def test_a_value_only_in_a_definition_is_not_seeded(self) -> None:
        """The parse already expanded its uses, so the edit would be inert."""
        text = (
            BS + "newcommand{" + BS + "eff}{0.166}\n"
            + BS + "begin{tabular}{cc}\nEffect & 0.166 " + BS * 2 + "\n" + BS + "end{tabular}\n"
        )
        self.assertIsNone(seed_contradicted_number(text, self.parsed, self.rng()))

    def test_a_crossref_is_repointed_at_nothing(self) -> None:
        text = "See Table~" + BS + "ref{tab:main} for details."
        mutated, mutation = seed_broken_crossref(text, self.parsed, self.rng())
        self.assertIn("tab:main-seeded-missing", mutated)
        self.assertEqual(mutation.target_auditor, "crossref_auditor")

    def test_an_unknown_citation_key_is_added(self) -> None:
        text = BS + "section{Results}\nThe effect is large.\n"
        mutated, mutation = seed_uncited_reference(text, self.parsed, self.rng())
        self.assertIn("seededmissing2026", mutated)
        self.assertEqual(mutation.target_auditor, "reference_auditor")

    def test_seed_defects_records_what_it_did(self) -> None:
        (self.parsed / "full_text.md").write_text(
            BS + "section{Results}\n"
            "The estimated effect on test scores is 0.166 standard deviations.\n"
            "See Table~" + BS + "ref{tab:main}.\n"
            + BS + "begin{tabular}{cc}\nEffect & 0.166 " + BS * 2 + "\n" + BS + "end{tabular}\n",
            encoding="utf-8",
        )
        mutations = seed_defects(self.parsed)
        self.assertTrue(mutations)
        recorded = json.loads((self.parsed / "seeded_defects.json").read_text(encoding="utf-8"))
        self.assertEqual(len(recorded), len(mutations))
        self.assertTrue(all(entry["detected"] is False for entry in recorded))


class Matching(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reviews = Path(self.tmp.name)
        self.mutation = Mutation(
            defect_id="SEED-NUM-001",
            defect_class="numeric_contradiction",
            target_auditor="numerical_auditor",
            description="x",
            token="0.161",
            original="0.166",
        )

    def write_review(self, reviewer: str, findings: list[dict]) -> None:
        (self.reviews / f"{reviewer}.json").write_text(
            json.dumps({"reviewer": reviewer, "findings": findings}), encoding="utf-8"
        )

    def test_a_finding_quoting_the_seeded_value_counts(self) -> None:
        self.write_review("numerical_auditor", [
            {"id": "NUM-001", "finding_summary": "The text reports 0.161 but Table 2 reports 0.166."}
        ])
        [mutation] = match_findings(self.reviews, [self.mutation])
        self.assertTrue(mutation.detected)
        self.assertEqual(mutation.matched_findings, ["numerical_auditor:NUM-001"])

    def test_the_value_is_found_in_a_numeric_check_too(self) -> None:
        self.write_review("numerical_auditor", [
            {"id": "NUM-002", "finding_summary": "mismatch",
             "numeric_check": {"reported_value": "0.161", "expected_value": "0.166"}}
        ])
        [mutation] = match_findings(self.reviews, [self.mutation])
        self.assertTrue(mutation.detected)

    def test_an_unrelated_finding_does_not_count(self) -> None:
        self.write_review("numerical_auditor", [
            {"id": "NUM-003", "finding_summary": "The sample size is unclear."}
        ])
        [mutation] = match_findings(self.reviews, [self.mutation])
        self.assertFalse(mutation.detected)

    def test_unmatched_findings_are_counted_not_scored(self) -> None:
        self.write_review("numerical_auditor", [
            {"id": "NUM-001", "finding_summary": "reports 0.161 against 0.166"},
            {"id": "NUM-004", "finding_summary": "A genuine pre-existing problem."},
        ])
        mutations = match_findings(self.reviews, [self.mutation])
        self.assertEqual(unmatched_finding_count(self.reviews, mutations), 1)


class Reporting(unittest.TestCase):
    def test_the_report_states_its_own_limits(self) -> None:
        caught = Mutation("SEED-A", "numeric_contradiction", "numerical_auditor", "d", "0.161", "0.166")
        caught.matched_findings.append("numerical_auditor:NUM-001")
        caught.detected_by.append("numerical_auditor")
        missed = Mutation("SEED-B", "dangling_cross_reference", "crossref_auditor", "d", "tok", "orig")

        text = render_report([caught, missed], unmatched=4, paper_id="demo")
        self.assertIn("1 of 2 seeded defects were detected", text)
        self.assertIn("Not a false-positive rate", text)
        self.assertIn("4 finding(s) matched no seeded defect", text)
        self.assertIn("Not general reliability", text)
        self.assertIn("lower bound", text)
        self.assertIn("Reading a miss", text)

    def test_a_clean_sweep_omits_the_miss_guidance(self) -> None:
        caught = Mutation("SEED-A", "numeric_contradiction", "numerical_auditor", "d", "0.161", "0.166")
        caught.matched_findings.append("numerical_auditor:NUM-001")
        text = render_report([caught], unmatched=0, paper_id="demo")
        self.assertIn("1 of 1 seeded defects were detected", text)
        self.assertNotIn("Reading a miss", text)


if __name__ == "__main__":
    unittest.main()
