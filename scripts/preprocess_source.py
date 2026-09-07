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
# \InputIfFileExists{file}{then}{else} takes three groups; the trailing two must
# be consumed or their braces leak into the flattened text.
INPUT_IF = re.compile(r"\\InputIfFileExists\{([^}]+)\}")
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


def flatten(
    root: Path, seen: set[Path] | None = None, base: Path | None = None
) -> tuple[str, list[dict]]:
    """Expand \\input, \\include and \\InputIfFileExists recursively.

    LaTeX resolves an input path against the directory of the **main document**,
    not against the file doing the including. A section living in
    `paper/appendices/` that says `\\input{tables/foo}` means
    `paper/tables/foo.tex`. Resolving against the including file instead loses
    every such file silently, which for a repository-generated manuscript is
    most of the tables.

    `base` carries the main document's directory down the recursion. The
    including file's own directory stays as a fallback, since some projects rely
    on that habit and TEXINPUTS makes both arrangements compile.
    """
    seen = seen if seen is not None else set()
    root = root.resolve()
    base = (base or root.parent).resolve()
    if root in seen:
        return "", []
    seen.add(root)
    text = strip_comments(root.read_text(encoding="utf-8", errors="replace"))
    provenance = [{"file": root.name, "path": str(root)}]

    def resolve(target: str) -> Path | None:
        for directory in (base, root.parent):
            for candidate in (directory / target, directory / f"{target}.tex"):
                if candidate.is_file():
                    return candidate
        return None

    def inline(target_name: str) -> tuple[str, list[dict]]:
        target = resolve(target_name)
        if target is None:
            return f"\n%% MISSING INPUT: {target_name}\n", []
        if target.suffix.lower() not in {".tex", ".txt", ""}:
            return "", []
        inner, inner_prov = flatten(target, seen, base)
        return f"\n%% BEGIN {target.name}\n{inner}\n%% END {target.name}\n", inner_prov

    # Both forms in one left-to-right pass, so offsets stay consistent.
    events = [(m.start(), m.end(), m.group(1), False) for m in INPUT.finditer(text)]
    events += [(m.start(), m.end(), m.group(1), True) for m in INPUT_IF.finditer(text)]
    events.sort()

    out, cursor = [], 0
    for start, end, target_name, conditional in events:
        if start < cursor:
            continue
        out.append(text[cursor:start])
        body, prov = inline(target_name)
        out.append(body)
        provenance.extend(prov)
        cursor = end
        if conditional:
            # Consume the {then} and {else} groups so their braces do not leak.
            for _ in range(2):
                if cursor < len(text) and text[cursor] == "{":
                    cursor = match_group(text, cursor)
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


#: Zero-argument macro definitions, in both spellings LaTeX accepts. The two
#: forms are spelled out rather than made optional with `\{?...\}?`, because an
#: optional brace lets the engine backtrack past it and satisfy the `(?!\[)`
#: guard against the closing brace, so `\newcommand{\se}[1]{(#1)}` was read as
#: a value and every `\se` in the document became `(#1)`.
MACRO_DEF = re.compile(
    r"\\(new|renew|provide)command\*?\s*"
    r"(?:\{\\([A-Za-z@]+)\}|\\([A-Za-z@]+))"
    r"\s*(?!\[)"
)

#: A body holding a real command is a formatting macro; a body holding a number,
#: a word, or a percent sign is a value. `\%`, `\$` and spacing escapes are
#: allowed through because generated value files use them.
NON_VALUE_BODY = re.compile(r"\\[A-Za-z]{3,}")

#: Long bodies are prose or layout, not a reported figure.
MAX_VALUE_BODY = 120

#: Expansion passes, so a value macro defined in terms of another resolves.
#: Bounded because a cyclic definition would otherwise spin forever.
MACRO_PASSES = 3


def macro_definitions(text: str) -> tuple[dict[str, str], list[tuple[int, int]]]:
    """Value macros the document defines, and the spans that define them.

    Returns `(values, spans)`. The spans matter as much as the values: a
    definition names its own macro, so substituting inside one would rewrite
    `\\newcommand{\\x}{0.06}` into `\\newcommand{0.06}{0.06}`.

    Precedence follows LaTeX. `\\providecommand` only defines what is not
    already defined, so a generated `\\newcommand` beats the `\\providecommand`
    fallback a manuscript keeps so it still compiles before the numbers are
    built. Among real definitions the last wins, matching `\\renewcommand`.
    """
    values: dict[str, str] = {}
    provided: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for match in MACRO_DEF.finditer(text):
        kind = match.group(1)
        name = match.group(2) or match.group(3)
        brace = text.find("{", match.end())
        if brace == -1:
            continue
        close = match_group(text, brace)
        body = text[brace + 1 : close - 1].strip()
        spans.append((match.start(), close))
        if len(body) > MAX_VALUE_BODY or NON_VALUE_BODY.search(body):
            continue
        if kind == "provide":
            provided.setdefault(name, body)
        else:
            values[name] = body
    for name, body in provided.items():
        values.setdefault(name, body)
    return values, spans


def macro_pattern(names) -> re.Pattern | None:
    """Match any of `names` used as a macro, longest first so prefixes lose."""
    if not names:
        return None
    alternatives = "|".join(sorted(map(re.escape, names), key=len, reverse=True))
    return re.compile(r"\\(" + alternatives + r")(?![A-Za-z@])")


def resolve_nested(values: dict[str, str]) -> dict[str, str]:
    """Resolve values written in terms of other values, before touching the text.

    Doing this here rather than by re-running over the document is what keeps
    definitions safe: a definition still contains the name it defines, so a
    second unguarded pass over the text rewrites `\\newcommand{\\x}{0.06}` into
    `\\newcommand{0.06}{0.06}`. Resolving inside the values instead means the
    document needs exactly one pass.
    """
    pattern = macro_pattern(values)
    if pattern is None:
        return values
    resolved = dict(values)
    for _ in range(MACRO_PASSES):
        changed = False
        for name, body in list(resolved.items()):
            # Never let a macro expand itself; a cycle would grow without bound.
            rewritten = pattern.sub(
                lambda m: resolved[m.group(1)] if m.group(1) != name else m.group(0),
                body,
            )
            if rewritten != body:
                resolved[name] = rewritten
                changed = True
        if not changed:
            break
    return resolved


def expand_value_macros(text: str, values: dict[str, str], spans: list[tuple[int, int]]):
    """Substitute value macros everywhere except where they are defined.

    Returns `(expanded_text, counts)`. A manuscript that writes its results as
    generated definitions reads as number-free prose without this, so the
    numerical auditor sees a sentence with nothing in it to check. That is a
    silent failure: nothing errors, the audit is simply blind.

    Exactly one pass runs over the document, and it skips every definition
    span. Values are resolved against each other first.
    """
    values = resolve_nested(values)
    pattern = macro_pattern(values)
    if pattern is None:
        return text, {}

    counts: dict[str, int] = {}

    def substitute(segment: str) -> str:
        def replace(match):
            name = match.group(1)
            counts[name] = counts.get(name, 0) + 1
            return values[name]
        return pattern.sub(replace, segment)

    pieces = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        pieces.append(substitute(text[cursor:start]))
        pieces.append(text[start:end])
        cursor = end
    pieces.append(substitute(text[cursor:]))
    return "".join(pieces), counts


#: Where a table's rows actually live. Matching `table` alone misses the
#: starred two-column form, sideways floats, and `longtable`, which is not a
#: float at all but is how a table too long for one page is written.
TABLE_ENVIRONMENTS = ("table", "table*", "sidewaystable", "sidewaystable*", "longtable")
FIGURE_ENVIRONMENTS = ("figure", "figure*", "sidewaysfigure", "sidewaysfigure*")


def latex_floats(text: str, kinds) -> list[tuple[int, str, str]]:
    """(start index, body, environment) for each float, in document order."""
    if isinstance(kinds, str):
        kinds = (kinds,)
    found: list[tuple[int, str, str]] = []
    for kind in kinds:
        opener = re.compile(r"\\begin\{" + re.escape(kind) + r"\}")
        closer = "\\end{" + kind + "}"
        for match in opener.finditer(text):
            end = text.find(closer, match.end())
            if end == -1:
                continue
            found.append((match.start(), text[match.end() : end], kind))
    found.sort(key=lambda item: item[0])
    return found


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
    for i, (start, body, environment) in enumerate(latex_floats(text, TABLE_ENVIRONMENTS), start=1):
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
                "environment": environment,
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
    for i, (start, body, environment) in enumerate(latex_floats(text, FIGURE_ENVIRONMENTS), start=1):
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
                # How many images the figure asks for, which is knowable here.
                "graphics_count": len(graphics),
                # How many were extracted from a rendered page, which in source
                # mode is always none. Reporting the \includegraphics count in
                # this field claimed images existed that were never written to
                # parsed/figures/, so a reviewer could cite one that is not there.
                "embedded_image_count": 0,
                "embedded_images": [],
                "environment": environment,
                "status": "source",
                "source": "latex_source",
                "title": None,
            }
        )
    return inventory


def resolve_root(source: Path) -> Path:
    """Accept a root .tex file or a directory holding exactly one."""
    source = Path(source).resolve()
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
    return source


def discover_bibs(root: Path, explicit: list[str] | None = None) -> list[Path]:
    if explicit:
        return [Path(b).resolve() for b in explicit]
    return sorted(root.parent.glob("*.bib"))


def source_sha256(root: Path, bibs: list[Path] | None = None) -> str:
    """Digest of everything the parse depends on.

    The root file alone is the wrong thing to hash: a manuscript built from a
    repository is mostly \\input files, and an edit to any of them, or to the
    bibliography, changes the parse while leaving the root untouched. Resume
    compares this digest, so it has to cover the flattened text and each .bib.
    """
    text, _ = flatten(root)
    digest = hashlib.sha256(text.encode("utf-8"))
    for bib in (bibs if bibs is not None else discover_bibs(root)):
        if bib.exists():
            digest.update(b"\x00bib:" + bib.name.encode("utf-8") + b"\x00")
            digest.update(bib.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build parsed artifacts from LaTeX source rather than a PDF."
    )
    parser.add_argument("--source", required=True, help="Root .tex file, or a directory holding one")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--bib", action="append", default=None, help="Explicit .bib path; repeatable")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    source = resolve_root(Path(args.source))

    paths = paper_run_paths(repo, args.paper_id)
    parsed = paths.parsed_dir
    parsed.mkdir(parents=True, exist_ok=True)

    text, provenance = flatten(source)
    # Substitute value macros before anything reads the text. A manuscript that
    # writes its results as generated definitions is number-free prose to every
    # collector and every auditor otherwise, and nothing errors to say so.
    macro_values, macro_spans = macro_definitions(text)
    text, macro_counts = expand_value_macros(text, macro_values, macro_spans)
    (parsed / "full_text.md").write_text(text, encoding="utf-8")

    sections = collect_sections(text)
    numbers = collect_numbers(text)
    crossrefs = collect_crossrefs(text)
    citations = collect_citations(text)
    tables = collect_tables(text)
    figures = collect_figures(text)

    cited = {c["citation_text"].lower() for c in citations}
    bibs = discover_bibs(source, args.bib)
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
    # What was substituted, so a finding resting on an expanded number can be
    # traced back to the definition it came from.
    write_json(
        parsed / "macro_expansions.json",
        [
            {"macro": name, "value": macro_values[name], "expansions": count}
            for name, count in sorted(macro_counts.items())
        ],
    )
    write_json(parsed / "figures" / "embedded_image_inventory.json", [])

    undefined = sorted({c["label"] for c in crossrefs if c["source"] == "latex_ref"}
                       - set(LABEL.findall(text)))
    manifest = {
        "paper_id": args.paper_id,
        "mode": "source",
        "source_root": str(source),
        "source_root_sha256": sha256(source),
        # The key review_paper.py compares on resume; covers every included
        # file and bibliography, not just the root.
        "source_pdf_sha256": source_sha256(source, bibs),
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
            "macro_definition_count": len(macro_values),
            "macro_expansion_count": sum(macro_counts.values()),
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
