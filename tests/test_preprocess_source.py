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
    first_caption,
    flatten,
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


if __name__ == "__main__":
    unittest.main()
