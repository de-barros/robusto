"""Build the parsed artifacts from LaTeX source instead of a rendered PDF.

`preprocess_pdf.py` recovers structure from a compiled document. When the
manuscript lives in a repository that generates it, most of what that recovery
is *reconstructing* is already present exactly:

    numbers        the generated table snippets hold them as text
    cross-refs     LaTeX resolved every \\ref into the .aux
    citations      \\cite keys are literal; the .bib holds the entries
    tables         the snippet is the table, before it became glyphs

So this reads the source. It emits the same artifact names as the PDF parser,
so every auditor and the whole downstream pipeline work unchanged.

What it deliberately does not emit:

    page_index.json                  source has no pages
    figures/embedded_image_inventory.json   nothing is embedded yet

Both are written empty, and `manifest.json` records `mode: "source"` so a
reviewer can tell why a page number is null. Findings anchor on section and
quoted text instead, which the reviewer_output schema already allows.

Use `--build` on review_paper.py when you want the compiled article as the
referee will receive it: layout, figure legibility, and page budget exist only
after typesetting, and this mode cannot see them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_paths import paper_run_paths  # noqa: E402

COMMENT = re.compile(r"(?<!\\)%.*$", re.M)
INPUT = re.compile(r"\\(?:input|include)\{([^}]+)\}")
SECTION = re.compile(r"\\(sub)*section\*?\{([^}]*)\}")
CITE = re.compile(r"\\cite[a-zA-Z]*\*?(?:\[[^]]*\])*\{([^}]*)\}")
REF = re.compile(r"\\(?:eq|page|name|c)?ref\*?\{([^}]*)\}")
LABEL = re.compile(r"\\label\{([^}]*)\}")
TEXTUAL_REF = re.compile(r"\b(Table|Figure|Fig\.|Eq\.|Equation|Appendix)~?\s?(S?[0-9]+[A-Za-z]?)")
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
BIB_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.M)
GRAPHIC = re.compile(r"\\includegraphics(?:\[[^]]*\])?\{([^}]+)\}")
CAPTION = re.compile(r"\\caption\*?\{", re.M)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_comments(text: str) -> str:
    return COMMENT.sub("", text)


def match_group(text: str, start: int) -> int:
    """Index just past the brace group opening at `start`."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif ch == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def flatten(root: Path, seen: set[Path] | None = None) -> tuple[str, list[dict]]:
    """Expand \\input and \\include recursively, recording provenance."""
    seen = seen if seen is not None else set()
    root = root.resolve()
    if root in seen:
        return "", []
    seen.add(root)
    text = strip_comments(root.read_text(encoding="utf-8", errors="replace"))
    provenance = [{"file": root.name, "path": str(root)}]

    def resolve(target: str) -> Path | None:
        for candidate in (root.parent / target, root.parent / f"{target}.tex"):
            if candidate.exists():
                return candidate
        return None

    out, cursor = [], 0
    for match in INPUT.finditer(text):
        out.append(text[cursor : match.start()])
        target = resolve(match.group(1))
        if target is None:
            out.append(f"\n%% MISSING INPUT: {match.group(1)}\n")
        elif target.suffix.lower() in {".tex", ".txt", ""}:
            inner, inner_prov = flatten(target, seen)
            out.append(f"\n%% BEGIN {target.name}\n{inner}\n%% END {target.name}\n")
            provenance.extend(inner_prov)
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out), provenance


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def context(text: str, start: int, end: int, width: int = 90) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - width) : end + width]).strip()


def collect_sections(text: str) -> list[dict]:
    sections = []
    for i, m in enumerate(SECTION.finditer(text), start=1):
        level = "subsection" if m.group(1) else "section"
        sections.append(
            {
                "section_id": f"S{i:03d}",
                "title": m.group(2).strip(),
                "level": level,
                "page": None,
                "page_label": None,
                "line_number": line_of(text, m.start()),
                "char_start": m.start(),
            }
        )
    return sections


def collect_numbers(text: str) -> list[dict]:
    return [
        {
            "page": None,
            "page_label": None,
            "number": m.group(0),
            "line_number": line_of(text, m.start()),
            "match_start": m.start(),
            "match_end": m.end(),
            "context": context(text, m.start(), m.end()),
        }
        for m in NUMBER.finditer(text)
    ]


def collect_crossrefs(text: str) -> list[dict]:
    found = []
    for m in REF.finditer(text):
        found.append(
            {
                "page": None,
                "page_label": None,
                "reference_text": m.group(0),
                "kind": "label_ref",
                "label": m.group(1),
                "line_number": line_of(text, m.start()),
                "match_start": m.start(),
                "match_end": m.end(),
                "context": context(text, m.start(), m.end()),
                "source": "latex_ref",
            }
        )
    for m in TEXTUAL_REF.finditer(text):
        found.append(
            {
                "page": None,
                "page_label": None,
                "reference_text": m.group(0),
                "kind": m.group(1),
                "label": m.group(2),
                "line_number": line_of(text, m.start()),
                "match_start": m.start(),
                "match_end": m.end(),
                "context": context(text, m.start(), m.end()),
                "source": "prose",
            }
        )
    return sorted(found, key=lambda r: r["match_start"])


def collect_citations(text: str) -> list[dict]:
    citations = []
    n = 0
    for m in CITE.finditer(text):
        for key in (k.strip() for k in m.group(1).split(",")):
            if not key:
                continue
            n += 1
            citations.append(
                {
                    "citation_id": f"C{n:03d}",
                    "page": None,
                    "page_label": None,
                    "citation_text": key,
                    "line_number": line_of(text, m.start()),
                    "match_start": m.start(),
                    "match_end": m.end(),
                    "context": context(text, m.start(), m.end()),
                    "source": "latex_cite",
                }
            )
    return citations


def collect_references(bib_paths: list[Path], cited: set[str]) -> list[dict]:
    """Entries from the .bib files, flagged by whether the manuscript cites them."""
    references = []
    for bib in bib_paths:
        raw = bib.read_text(encoding="utf-8", errors="replace")
        for i, m in enumerate(BIB_ENTRY.finditer(raw)):
            key = m.group(2)
            end = match_group(raw, raw.index("{", m.start()))
            entry = raw[m.start() : end]
            references.append(
                {
                    "reference_id": len(references) + 1,
                    "page": None,
                    "page_label": None,
                    "key": key,
                    "entry_type": m.group(1).lower(),
                    "raw": entry if len(entry) < 4000 else entry[:4000],
                    "cited_in_text": key.lower() in cited,
                    "source_file": bib.name,
                }
            )
    return references


def latex_floats(text: str, kind: str) -> list[tuple[int, str]]:
    """(start index, body) for each float of `kind`."""
    out = []
    for m in re.finditer(rf"\\begin\{{{kind}\}}", text):
        end = text.find(rf"\end{{{kind}}}", m.end())
        if end == -1:
            continue
        out.append((m.start(), text[m.end() : end]))
    return out


def first_caption(body: str) -> str | None:
    m = CAPTION.search(body)
    if not m:
        return None
    inner = body[m.end() - 1 :]
    close = match_group(inner, 0)
    caption = inner[1 : close - 1]
    caption = re.sub(r"\\[a-zA-Z]+\*?", " ", caption)
    caption = re.sub(r"[{}]", "", caption)
    return re.sub(r"\s+", " ", caption).strip() or None


def collect_tables(text: str) -> list[dict]:
    inventory = []
    for i, (start, body) in enumerate(latex_floats(text, "table"), start=1):
        label = LABEL.search(body)
        rows = body.count("\\\\")
        inventory.append(
            {
                "table_id": f"T{i:03d}",
                "table_label": label.group(1) if label else None,
                "caption": first_caption(body),
                "caption_source": "latex_caption",
                "page": None,
                "page_label": None,
                "line_number": line_of(text, start),
                "row_count": rows,
                "col_count": None,
                "status": "source",
                "source": "latex_source",
                "inventory_role": "labelled" if label else "unlabeled_candidate",
                "quality_flags": [],
                "body": body.strip(),
            }
        )
    return inventory


def collect_figures(text: str) -> list[dict]:
    inventory = []
    for i, (start, body) in enumerate(latex_floats(text, "figure"), start=1):
        label = LABEL.search(body)
        graphics = GRAPHIC.findall(body)
        inventory.append(
            {
                "figure_id": f"F{i:03d}",
                "figure_label": label.group(1) if label else None,
                "caption": first_caption(body),
                "caption_source": "latex_caption",
                "page": None,
                "page_label": None,
                "line_number": line_of(text, start),
                "graphics": graphics,
                "embedded_image_count": len(graphics),
                "embedded_images": [],
                "status": "source",
                "source": "latex_source",
                "title": None,
            }
        )
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build parsed artifacts from LaTeX source rather than a PDF."
    )
    parser.add_argument("--source", required=True, help="Root .tex file, or a directory holding one")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--bib", action="append", default=None, help="Explicit .bib path; repeatable")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    source = Path(args.source).resolve()
    if source.is_dir():
        roots = [p for p in sorted(source.glob("*.tex")) if "\\documentclass" in
                 p.read_text(encoding="utf-8", errors="replace")[:4000].replace("\r", "")]
        if not roots:
            raise SystemExit(f"No .tex file with a \\documentclass found in {source}")
        if len(roots) > 1:
            names = ", ".join(p.name for p in roots)
            raise SystemExit(f"Multiple candidate roots in {source}: {names}. Pass one explicitly.")
        source = roots[0]
    if not source.exists():
        raise SystemExit(f"Source not found: {source}")

    paths = paper_run_paths(repo, args.paper_id)
    parsed = paths.parsed_dir
    parsed.mkdir(parents=True, exist_ok=True)

    text, provenance = flatten(source)
    (parsed / "full_text.md").write_text(text, encoding="utf-8")

    sections = collect_sections(text)
    numbers = collect_numbers(text)
    crossrefs = collect_crossrefs(text)
    citations = collect_citations(text)
    tables = collect_tables(text)
    figures = collect_figures(text)

    cited = {c["citation_text"].lower() for c in citations}
    if args.bib:
        bibs = [Path(b).resolve() for b in args.bib]
    else:
        bibs = sorted(source.parent.glob("*.bib"))
    references = collect_references([b for b in bibs if b.exists()], cited)

    write_json(parsed / "sections.json", sections)
    write_json(parsed / "numbers_in_text.json", numbers)
    write_json(parsed / "crossrefs.json", crossrefs)
    write_json(parsed / "in_text_citations.json", citations)
    write_json(parsed / "reference_list.json", references)
    write_json(parsed / "tables" / "table_inventory.json", tables)
    write_json(parsed / "figures" / "figure_inventory.json", figures)
    # No pages and no embedded images exist before typesetting. Written empty so
    # every downstream consumer finds the file it expects.
    write_json(parsed / "page_index.json", [])
    write_json(parsed / "figures" / "embedded_image_inventory.json", [])

    undefined = sorted({c["label"] for c in crossrefs if c["source"] == "latex_ref"}
                       - set(LABEL.findall(text)))
    manifest = {
        "paper_id": args.paper_id,
        "mode": "source",
        "source_root": str(source),
        "source_root_sha256": sha256(source),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "tool_versions": {"python": sys.version},
        "settings": {
            "project_root": str(repo),
            "expanded_files": [p["file"] for p in provenance],
            "bib_files": [b.name for b in bibs if b.exists()],
        },
        "summary": {
            "page_count": 0,
            "section_count": len(sections),
            "citation_candidate_count": len(citations),
            "reference_count": len(references),
            "cited_reference_count": sum(1 for r in references if r["cited_in_text"]),
            "numeric_claim_candidate_count": len(numbers),
            "crossref_count": len(crossrefs),
            "undefined_reference_labels": undefined,
            "table_count": len(tables),
            "figure_count": len(figures),
            "embedded_image_count": 0,
        },
        "limitations": [
            "No page numbers: findings anchor on section and quoted text.",
            "No layout, figure legibility, or page-budget evidence. Use --build for those.",
            "Table and figure bodies are LaTeX source, not rendered output.",
        ],
    }
    write_json(parsed / "manifest.json", manifest)

    print(json.dumps({"status": "ok", "paper_id": args.paper_id,
                      "parsed_dir": str(parsed), "mode": "source",
                      "summary": manifest["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
