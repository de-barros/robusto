"""Tests for the LaTeX source front-end.

Source mode exists because a repository that generates a manuscript already
holds exactly what the PDF parser has to reconstruct. These tests pin the
things that would silently degrade if that stopped being true: \\input
expansion, comment stripping, and the fact that a reference recovered from a
.bib is the entry itself rather than a guess at one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from preprocess_source import (  # noqa: E402
    collect_citations,
    collect_crossrefs,
    collect_figures,
    collect_numbers,
    collect_references,
    collect_sections,
    collect_tables,
    expand_value_macros,
    first_caption,
    flatten,
    macro_definitions,
    strip_comments,
)


class Flattening(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, body: str) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def test_input_is_expanded_recursively(self) -> None:
        self.write("inner.tex", "INNER BODY")
        self.write("mid.tex", "MID \\input{inner}")
        root = self.write("root.tex", "ROOT \\input{mid}")
        text, provenance = flatten(root)
        self.assertIn("ROOT", text)
        self.assertIn("MID", text)
        self.assertIn("INNER BODY", text)
        self.assertEqual([p["file"] for p in provenance], ["root.tex", "mid.tex", "inner.tex"])

    def test_missing_input_is_marked_not_silently_dropped(self) -> None:
        root = self.write("root.tex", "A \\input{nowhere} B")
        text, _ = flatten(root)
        self.assertIn("MISSING INPUT: nowhere", text)
        self.assertIn("A", text)
        self.assertIn("B", text)

    def test_a_cycle_terminates(self) -> None:
        self.write("a.tex", "A \\input{b}")
        self.write("b.tex", "B \\input{a}")
        text, _ = flatten(self.dir / "a.tex")
        self.assertIn("A", text)
        self.assertIn("B", text)

    def test_paths_resolve_from_the_main_document_not_the_including_file(self) -> None:
        """LaTeX's actual rule. Getting this wrong loses most of a repo's tables."""
        self.write("tables/lasso.tex", "LASSO TABLE BODY")
        self.write("appendices/A.tex", "APPENDIX \\input{tables/lasso.tex}")
        root = self.write("root.tex", "ROOT \\input{appendices/A}")
        text, provenance = flatten(root)
        self.assertIn("LASSO TABLE BODY", text)
        self.assertIn("lasso.tex", [p["file"] for p in provenance])

    def test_including_file_directory_still_works_as_a_fallback(self) -> None:
        self.write("parts/local.tex", "LOCAL BODY")
        self.write("parts/section.tex", "SECTION \\input{local}")
        root = self.write("root.tex", "ROOT \\input{parts/section}")
        text, _ = flatten(root)
        self.assertIn("LOCAL BODY", text)

    def test_input_if_file_exists_is_expanded_and_its_branches_consumed(self) -> None:
        self.write("counts.tex", "COUNTS BODY")
        root = self.write(
            "root.tex", "A \\InputIfFileExists{counts.tex}{THEN}{ELSE} B"
        )
        text, _ = flatten(root)
        self.assertIn("COUNTS BODY", text)
        self.assertIn("A", text)
        self.assertIn("B", text)
        for leaked in ("THEN", "ELSE"):
            self.assertNotIn(leaked, text)

    def test_generated_table_snippets_are_pulled_in(self) -> None:
        """The point of source mode: the numbers arrive as text, not glyphs."""
        self.write("table1.txt", "Treatment & 0.90 & (0.07) \\\\")
        root = self.write("root.tex", "\\begin{table}\\input{table1.txt}\\end{table}")
        text, _ = flatten(root)
        self.assertIn("0.90", text)
        self.assertIn("(0.07)", text)


class Stripping(unittest.TestCase):
    def test_comments_go(self) -> None:
        self.assertNotIn("secret", strip_comments("visible % secret\nnext"))

    def test_escaped_percent_survives(self) -> None:
        self.assertIn("95\\%", strip_comments("95\\% confidence % a comment"))


class Collectors(unittest.TestCase):
    def test_sections(self) -> None:
        got = collect_sections("\\section*{Results}\ntext\n\\subsection*{Detail}")
        self.assertEqual([s["title"] for s in got], ["Results", "Detail"])
        self.assertEqual([s["level"] for s in got], ["section", "subsection"])

    def test_numbers_carry_context_and_no_page(self) -> None:
        got = collect_numbers("learning rose by 0.90 standard deviations")
        self.assertEqual(got[0]["number"], "0.90")
        self.assertIsNone(got[0]["page"])
        self.assertIn("standard deviations", got[0]["context"])

    def test_crossrefs_from_both_labels_and_prose(self) -> None:
        got = collect_crossrefs("see \\ref{tab:main} and also Table 3")
        sources = {c["source"] for c in got}
        self.assertEqual(sources, {"latex_ref", "prose"})
        self.assertIn("tab:main", [c["label"] for c in got])
        self.assertIn("3", [c["label"] for c in got])

    def test_multi_key_cite_becomes_one_record_per_key(self) -> None:
        got = collect_citations("\\cite{smith2020,jones2021}")
        self.assertEqual([c["citation_text"] for c in got], ["smith2020", "jones2021"])

    def test_tables_and_figures_capture_label_and_caption(self) -> None:
        tex = (
            "\\begin{table}\\caption{\\textbf{Balance.} Detail here.}"
            "\\label{tab:bal}\\input{t.txt}\\end{table}\n"
            "\\begin{figure}\\includegraphics{fig1.pdf}"
            "\\caption{Trends.}\\label{fig:one}\\end{figure}"
        )
        tables = collect_tables(tex)
        figures = collect_figures(tex)
        self.assertEqual(tables[0]["table_label"], "tab:bal")
        self.assertIn("Balance", tables[0]["caption"])
        self.assertEqual(figures[0]["figure_label"], "fig:one")
        self.assertEqual(figures[0]["graphics"], ["fig1.pdf"])

    def test_caption_with_nested_braces(self) -> None:
        body = "\\caption{\\textbf{Bold {nested} title.} Rest.}"
        self.assertIn("nested", first_caption(body))


class References(unittest.TestCase):
    def test_cited_entries_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bib = Path(tmp) / "refs.bib"
            bib.write_text(
                "@article{used2020, title={Used}, author={A}}\n"
                "@book{unused1999, title={Unused}, author={B}}\n",
                encoding="utf-8",
            )
            got = collect_references([bib], {"used2020"})
            flags = {r["key"]: r["cited_in_text"] for r in got}
            self.assertTrue(flags["used2020"])
            self.assertFalse(flags["unused1999"])

    def test_entry_is_carried_verbatim_not_reconstructed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bib = Path(tmp) / "refs.bib"
            bib.write_text("@article{k, title={A {Braced} Title}, year={2020}}\n", encoding="utf-8")
            got = collect_references([bib], {"k"})
            self.assertIn("{Braced}", got[0]["raw"])


class ValueMacros(unittest.TestCase):
    """Expansion of generated value macros.

    Economics manuscripts routinely inject their results as a Stata-generated
    file of `\\newcommand`s. Without expansion the prose carries no numbers at
    all, so the numerical auditor reads a sentence with nothing in it to check
    and reports nothing wrong. Nothing errors; the audit is simply blind.
    """

    def expand(self, text: str) -> tuple[str, dict]:
        values, spans = macro_definitions(text)
        return expand_value_macros(text, values, spans)

    def test_a_value_reaches_the_prose(self) -> None:
        text = (
            "\\newcommand{\\effect}{-0.17}\n"
            "The coefficient is $\\effect$ standard deviations.\n"
        )
        expanded, counts = self.expand(text)
        self.assertIn("$-0.17$ standard deviations", expanded)
        self.assertEqual(counts, {"effect": 1})

    def test_the_definition_is_not_rewritten(self) -> None:
        """The bug this test exists for.

        A definition contains the name it defines, so any substitution pass
        that does not skip definition spans turns
        `\\newcommand{\\effect}{-0.17}` into `\\newcommand{-0.17}{-0.17}`,
        which destroys the definition and every later reader of it.
        """
        text = (
            "\\newcommand{\\effect}{-0.17}\n"
            "The coefficient is $\\effect$ standard deviations.\n"
        )
        expanded, _ = self.expand(text)
        self.assertIn("\\newcommand{\\effect}{-0.17}", expanded)
        self.assertNotIn("\\newcommand{-0.17}", expanded)

    def test_a_real_definition_beats_the_providecommand_fallback(self) -> None:
        """Manuscripts keep a placeholder so they compile before results exist.

        Getting this backwards is worse than not expanding at all: the prose
        would read "the coefficient is [run master.do]", which looks like
        content rather than like a missing value.
        """
        text = (
            "\\providecommand{\\effect}{\\textbf{[run master.do]}}\n"
            "\\newcommand{\\effect}{-0.17}\n"
            "The coefficient is $\\effect$.\n"
        )
        expanded, _ = self.expand(text)
        self.assertIn("The coefficient is $-0.17$.", expanded)
        self.assertNotIn("[run master.do]", expanded.split("\n", 1)[1])

    def test_a_providecommand_alone_is_used_when_nothing_overrides_it(self) -> None:
        text = "\\providecommand{\\n}{1,240}\nWe study $\\n$ schools.\n"
        expanded, _ = self.expand(text)
        self.assertIn("We study $1,240$ schools.", expanded)

    def test_macros_taking_arguments_are_left_alone(self) -> None:
        """A macro with arguments is a formatting helper, not a value."""
        text = "\\newcommand{\\se}[1]{(#1)}\nThe estimate is 0.4 \\se{0.1}.\n"
        expanded, counts = self.expand(text)
        self.assertIn("\\se{0.1}", expanded)
        self.assertEqual(counts, {})

    def test_formatting_macros_are_left_alone(self) -> None:
        """A body holding a real command is layout, not a reported figure."""
        text = (
            "\\renewcommand{\\thetable}{A\\arabic{table}}\n"
            "\\renewcommand{\\contentsname}{Contents}\n"
            "See \\thetable{} for details.\n"
        )
        expanded, counts = self.expand(text)
        self.assertIn("\\thetable", expanded)
        self.assertNotIn("thetable", counts)

    def test_a_value_defined_from_another_value_resolves(self) -> None:
        text = "\\newcommand{\\a}{0.5}\n\\newcommand{\\b}{\\a}\nResult $\\b$.\n"
        expanded, _ = self.expand(text)
        self.assertIn("Result $0.5$.", expanded)

    def test_a_cyclic_definition_terminates(self) -> None:
        """Bounded passes, so a cycle cannot spin or grow without limit."""
        text = "\\newcommand{\\a}{\\b}\n\\newcommand{\\b}{\\a}\nValue $\\a$.\n"
        expanded, _ = self.expand(text)
        self.assertLess(len(expanded), 400)

    def test_a_longer_name_is_not_shadowed_by_a_shorter_one(self) -> None:
        text = "\\newcommand{\\ab}{1}\n\\newcommand{\\abc}{2}\nBoth $\\ab$ and $\\abc$.\n"
        expanded, _ = self.expand(text)
        self.assertIn("Both $1$ and $2$.", expanded)

    def test_a_macro_is_not_matched_inside_a_longer_name(self) -> None:
        text = "\\newcommand{\\ab}{1}\nThe \\abdefined command stays.\n"
        expanded, _ = self.expand(text)
        self.assertIn("\\abdefined", expanded)

    def test_an_overlong_body_is_treated_as_prose(self) -> None:
        text = "\\newcommand{\\blurb}{" + "word " * 40 + "}\nSee $\\blurb$.\n"
        expanded, counts = self.expand(text)
        self.assertEqual(counts, {})
        self.assertIn("$\\blurb$", expanded)


class FloatEnvironments(unittest.TestCase):
    """Tables and figures live in more environments than the plain float.

    Missing `sidewaystable` and `longtable` silently shrank Avanti's inventory
    from 25 tables to 21, so four exhibits were invisible to every auditor.
    """

    def test_every_table_environment_is_collected(self) -> None:
        text = (
            "\\begin{table}\\caption{One}\\label{t:1}\\end{table}\n"
            "\\begin{table*}\\caption{Two}\\label{t:2}\\end{table*}\n"
            "\\begin{sidewaystable}\\caption{Three}\\label{t:3}\\end{sidewaystable}\n"
            "\\begin{longtable}\\caption{Four}\\label{t:4}\\end{longtable}\n"
        )
        tables = collect_tables(text)
        self.assertEqual(len(tables), 4)
        self.assertEqual(
            [t["environment"] for t in tables],
            ["table", "table*", "sidewaystable", "longtable"],
        )
        self.assertEqual([t["table_label"] for t in tables], ["t:1", "t:2", "t:3", "t:4"])

    def test_tables_are_numbered_in_document_order(self) -> None:
        text = (
            "\\begin{longtable}\\label{first}\\end{longtable}\n"
            "\\begin{table}\\label{second}\\end{table}\n"
        )
        tables = collect_tables(text)
        self.assertEqual([t["table_label"] for t in tables], ["first", "second"])
        self.assertEqual([t["table_id"] for t in tables], ["T001", "T002"])

    def test_figure_environments_are_collected(self) -> None:
        text = (
            "\\begin{figure}\\includegraphics{a.pdf}\\label{f:1}\\end{figure}\n"
            "\\begin{sidewaysfigure}\\includegraphics{b.pdf}\\label{f:2}\\end{sidewaysfigure}\n"
        )
        figures = collect_figures(text)
        self.assertEqual(len(figures), 2)
        self.assertEqual([f["environment"] for f in figures], ["figure", "sidewaysfigure"])

    def test_source_mode_claims_no_extracted_images(self) -> None:
        """Nothing is written to parsed/figures/, so the count must be zero.

        Reporting the \\includegraphics count as embedded_image_count invited a
        reviewer to cite an extracted image that does not exist.
        """
        text = "\\begin{figure}\\includegraphics{a.pdf}\\includegraphics{b.pdf}\\end{figure}\n"
        figure = collect_figures(text)[0]
        self.assertEqual(figure["embedded_image_count"], 0)
        self.assertEqual(figure["embedded_images"], [])
        self.assertEqual(figure["graphics_count"], 2)
        self.assertEqual(figure["graphics"], ["a.pdf", "b.pdf"])


class SectionTitles(unittest.TestCase):
    """Section and subsection title extraction.

    A \\label nested inside \\section{...} has its own closing brace, so a
    title regex that stops at the first `}` truncates the title and leaves an
    unbalanced `\\label{sec:foo` fragment glued to the end of it. Found by the
    preflight auditor on a real manuscript, on six of Avanti's headings.
    """

    def test_a_nested_label_does_not_truncate_the_title(self) -> None:
        text = r"\section{Robustness and heterogeneity \label{sec:robhet}}"
        sections = collect_sections(text)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["title"], "Robustness and heterogeneity")
        self.assertNotIn("{", sections[0]["title"])
        self.assertNotIn("}", sections[0]["title"])
        self.assertNotIn("label", sections[0]["title"].lower())

    def test_the_label_is_captured_rather_than_discarded(self) -> None:
        text = r"\section{Results \label{sec:rests}}"
        sections = collect_sections(text)
        self.assertEqual(sections[0]["section_label"], "sec:rests")

    def test_a_title_without_a_label_has_none(self) -> None:
        text = r"\section{Introduction}"
        sections = collect_sections(text)
        self.assertIsNone(sections[0]["section_label"])

    def test_subsections_are_distinguished_from_sections(self) -> None:
        text = r"\section{One}" + "\n" + r"\subsection{Two \label{sec:two}}"
        sections = collect_sections(text)
        self.assertEqual([s["level"] for s in sections], ["section", "subsection"])
        self.assertEqual(sections[1]["title"], "Two")
        self.assertEqual(sections[1]["section_label"], "sec:two")

    def test_a_starred_section_is_still_collected(self) -> None:
        text = r"\section*{Acknowledgements}"
        sections = collect_sections(text)
        self.assertEqual(sections[0]["title"], "Acknowledgements")

    def test_formatting_macros_in_a_title_are_reduced_to_plain_text(self) -> None:
        text = r"\section{The \textbf{ITT} Effect \label{sec:itt}}"
        sections = collect_sections(text)
        self.assertEqual(sections[0]["title"], "The ITT Effect")

    def test_sections_are_numbered_and_located_in_document_order(self) -> None:
        text = r"\section{First}" + "\n\n" + r"\section{Second}"
        sections = collect_sections(text)
        self.assertEqual([s["section_id"] for s in sections], ["S001", "S002"])
        self.assertLess(sections[0]["line_number"], sections[1]["line_number"])

    def test_a_brace_inside_a_double_nested_macro_still_balances(self) -> None:
        """The general case the bug was an instance of: any nested {...} group."""
        text = r"\section{Effects on \emph{test scores} \label{sec:eff}}"
        sections = collect_sections(text)
        self.assertEqual(sections[0]["title"], "Effects on test scores")
        self.assertEqual(sections[0]["section_label"], "sec:eff")


if __name__ == "__main__":
    unittest.main()
