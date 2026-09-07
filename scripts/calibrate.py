"""Measure whether the panel catches defects that are demonstrably there.

A review that reports nothing is ambiguous: the paper may be clean, or the
auditors may have missed what is wrong. Nothing in the report distinguishes
those, and the second reading is the one that matters. Calibration resolves it
by putting defects into the manuscript on purpose and checking they come back.

The method is seeded-defect detection, not accept/reject scoring. Simulated
peer reviewers can be calibrated against gold accept/reject labels, reported as
a false-negative and false-positive rate. That measurement is unavailable here
by design: robusto's editor is forbidden from recommending acceptance, so there
is no decision to score. What it does emit is findings, so the question with an
answer is whether a known defect produces one.

Each mutation edits a copy of the parsed artifacts, never the run's own, and
records exactly what it changed. Only the auditors whose remit covers the
seeded classes are run, which is what makes this affordable: four or five calls
rather than a second full panel.

What this measures, precisely: **recall on seeded defects of these classes, in
this manuscript, on this run.** It does not measure a false-positive rate. A
finding that matches no seeded defect is not thereby wrong, since the paper has
its own real defects, so unmatched findings are counted and reported as
unmatched rather than scored as errors. Nor does detection generalise beyond
the classes seeded: catching a contradicted coefficient says nothing about
whether an unstated identification assumption would be caught.
"""

from __future__ import annotations

import json
import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

#: Seeded defects are drawn deterministically so a rerun on the same parse
#: measures the same thing. Pass a different seed to sample other targets.
DEFAULT_SEED = 20260907

#: A mutated number must not collide with the original, and must stay
#: recognisably a number of the same shape, so the defect is a contradiction
#: rather than a parse error.
_DIGIT_SHIFT = {"0": "8", "1": "7", "2": "9", "3": "8", "4": "9",
                "5": "2", "6": "1", "7": "3", "8": "4", "9": "6"}


@dataclass
class Mutation:
    """One seeded defect, and what should notice it."""

    defect_id: str
    defect_class: str
    target_auditor: str
    description: str
    #: Text that a finding about this defect is expected to quote or restate.
    token: str
    original: str
    line_number: int | None = None
    detected_by: list[str] = field(default_factory=list)
    matched_findings: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return bool(self.matched_findings)

    def to_json(self) -> dict:
        return {
            "defect_id": self.defect_id,
            "defect_class": self.defect_class,
            "target_auditor": self.target_auditor,
            "description": self.description,
            "seeded_value": self.token,
            "original_value": self.original,
            "line_number": self.line_number,
            "detected": self.detected,
            "detected_by": sorted(set(self.detected_by)),
            "matched_findings": self.matched_findings,
        }


def shift_number(value: str) -> str:
    """Change a number's digits so it contradicts its source, same shape."""
    digits = [character for character in value if character.isdigit()]
    if not digits:
        return value
    last = max(index for index, ch in enumerate(value) if ch.isdigit())
    return value[:last] + _DIGIT_SHIFT[value[last]] + value[last + 1 :]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


#: Environments whose contents are table cells rather than prose. A number
#: edited inside one of these is not the defect this seeder intends: the point
#: is a prose claim that contradicts the table it cites, so the prose side has
#: to be the side that moves.
TABULAR_ENVIRONMENTS = ("tabular", "tabular*", "tabularx", "longtable", "array")


def tabular_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges covered by any table-cell environment."""
    spans: list[tuple[int, int]] = []
    for environment in TABULAR_ENVIRONMENTS:
        opener = "\\begin{" + environment + "}"
        closer = "\\end{" + environment + "}"
        start = text.find(opener)
        while start != -1:
            end = text.find(closer, start)
            if end == -1:
                break
            spans.append((start, end + len(closer)))
            start = text.find(opener, end)
    return spans


def _inside(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


#: Commands whose argument is a definition rather than a claim. Editing the
#: body of one of these is inert: the parse already substituted the macro's
#: uses, so the prose keeps the original value and nothing contradicts anything.
DEFINITION_COMMANDS = ("newcommand", "renewcommand", "providecommand", "def", "setlength")

#: Below this many word-like tokens, a line is a table row, a definition or
#: markup rather than a sentence making a claim. A cheap catch-all for
#: non-prose regions beyond the two that have actually bitten.
MIN_PROSE_WORDS = 5


def line_at(text: str, index: int) -> str:
    """The whole source line containing `index`."""
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start : end if end != -1 else len(text)]


def is_prose_line(line: str) -> bool:
    if any(("\\" + command) in line for command in DEFINITION_COMMANDS):
        return False
    return len(re.findall(r"[A-Za-z]{2,}", line)) >= MIN_PROSE_WORDS


def seed_contradicted_number(text: str, parsed: Path, rng: random.Random) -> tuple[str, Mutation] | None:
    """Change a number in the prose so it contradicts the table it came from.

    The number must appear inside a table and exactly once outside every table,
    so the edit creates a genuine disagreement between a prose claim and the
    exhibit behind it. Editing the table cell instead would either contradict
    nothing or move the very value the prose is checked against, and a defect
    that contradicts nothing measures the seeder rather than the panel.
    """
    inventory_path = parsed / "tables" / "table_inventory.json"
    if not inventory_path.is_file():
        return None
    tables = json.loads(inventory_path.read_text(encoding="utf-8"))
    table_numbers = set()
    for table in tables:
        table_numbers.update(re.findall(r"\d+\.\d+", table.get("body") or ""))
    if not table_numbers:
        return None

    spans = tabular_spans(text)
    candidates = []
    for number in sorted(table_numbers):
        pattern = re.compile(r"(?<![\w.])" + re.escape(number) + r"(?![\w.])")
        outside = [
            match
            for match in pattern.finditer(text)
            if not _inside(match.start(), spans) and is_prose_line(line_at(text, match.start()))
        ]
        inside = any(_inside(match.start(), spans) for match in pattern.finditer(text))
        if inside and len(outside) == 1:
            candidates.append((number, outside[0]))
    if not candidates:
        return None

    number, match = candidates[rng.randrange(len(candidates))]
    replacement = shift_number(number)
    if replacement == number:
        return None
    mutated = text[: match.start()] + replacement + text[match.end() :]
    return mutated, Mutation(
        defect_id="SEED-NUM-001",
        defect_class="numeric_contradiction",
        target_auditor="numerical_auditor",
        description=(
            f"A value stated in the prose as {number} was changed to {replacement}. "
            f"The table it is drawn from still reports {number}."
        ),
        token=replacement,
        original=number,
        line_number=_line_of(text, match.start()),
    )


def seed_broken_crossref(text: str, parsed: Path, rng: random.Random) -> tuple[str, Mutation] | None:
    """Point a cross-reference at a label that does not exist."""
    crossrefs_path = parsed / "crossrefs.json"
    if not crossrefs_path.is_file():
        return None
    crossrefs = [
        item for item in json.loads(crossrefs_path.read_text(encoding="utf-8"))
        if item.get("source") == "latex_ref" and item.get("label")
    ]
    if not crossrefs:
        return None

    chosen = crossrefs[rng.randrange(len(crossrefs))]
    original_label = chosen["label"]
    broken_label = f"{original_label}-seeded-missing"
    needle = "\\ref{" + original_label + "}"
    index = text.find(needle)
    if index == -1:
        return None
    replacement = "\\ref{" + broken_label + "}"
    mutated = text[:index] + replacement + text[index + len(needle) :]
    return mutated, Mutation(
        defect_id="SEED-XREF-001",
        defect_class="dangling_cross_reference",
        target_auditor="crossref_auditor",
        description=(
            f"A cross-reference to {original_label} was repointed at "
            f"{broken_label}, which is defined nowhere in the manuscript."
        ),
        token=broken_label,
        original=original_label,
        line_number=_line_of(text, index),
    )


def seed_uncited_reference(text: str, parsed: Path, rng: random.Random) -> tuple[str, Mutation] | None:
    """Cite a key that the bibliography does not contain."""
    references_path = parsed / "reference_list.json"
    if not references_path.is_file():
        return None
    known = {
        str(entry.get("citation_key") or entry.get("key") or "")
        for entry in json.loads(references_path.read_text(encoding="utf-8"))
    }
    invented = "seededmissing2026"
    if invented in known:
        return None

    anchor = re.search(r"(?m)^\\section\{", text)
    if anchor is None:
        return None
    insert_at = text.find("\n", anchor.end())
    if insert_at == -1:
        return None
    sentence = (
        "\nA further result along these lines is reported elsewhere "
        "\\citep{" + invented + "}.\n"
    )
    mutated = text[:insert_at] + sentence + text[insert_at:]
    return mutated, Mutation(
        defect_id="SEED-REF-001",
        defect_class="citation_missing_from_bibliography",
        target_auditor="reference_auditor",
        description=(
            f"A citation to {invented} was added to the text. No such entry "
            "exists in the bibliography."
        ),
        token=invented,
        original="(none)",
        line_number=_line_of(text, insert_at),
    )


#: Order matters only for reproducibility; each seeder edits the text in turn.
SEEDERS = (
    seed_contradicted_number,
    seed_broken_crossref,
    seed_uncited_reference,
)


def seed_defects(parsed: Path, *, seed: int = DEFAULT_SEED) -> list[Mutation]:
    """Apply every seeder that finds a target, rewriting `parsed/full_text.md`.

    Returns the mutations actually applied. A seeder that finds no suitable
    target is skipped rather than forced, since a manufactured target would
    measure the seeder rather than the panel.
    """
    text_path = parsed / "full_text.md"
    text = text_path.read_text(encoding="utf-8", errors="replace")
    rng = random.Random(seed)
    applied: list[Mutation] = []
    for seeder in SEEDERS:
        result = seeder(text, parsed, rng)
        if result is None:
            continue
        text, mutation = result
        applied.append(mutation)
    text_path.write_text(text, encoding="utf-8")
    (parsed / "seeded_defects.json").write_text(
        json.dumps([m.to_json() for m in applied], indent=2) + "\n", encoding="utf-8"
    )
    return applied


def finding_haystack(finding: dict) -> str:
    """Every place a finding could name the value it is about."""
    parts = [
        str(finding.get("finding_summary") or ""),
        str(finding.get("claim_text") or ""),
        str(finding.get("evidence_summary") or ""),
        str(finding.get("suggested_fix") or ""),
    ]
    location = finding.get("location") or {}
    parts.append(str(location.get("text_quote") or ""))
    for source in finding.get("source_objects") or []:
        if isinstance(source, dict):
            parts.append(str(source.get("text_quote") or ""))
            parts.append(str(source.get("label") or ""))
    check = finding.get("numeric_check")
    if isinstance(check, dict):
        parts.extend(str(check.get(key) or "") for key in ("reported_value", "expected_value", "recomputation_notes"))
    for link in finding.get("claim_evidence_links") or []:
        if isinstance(link, dict):
            parts.append(str(link.get("claim_text") or ""))
    return "\n".join(parts)


def match_findings(reviews_dir: Path, mutations: list[Mutation]) -> list[Mutation]:
    """Attribute findings to seeded defects by the value each one names.

    Matching is heuristic: a finding counts as detecting a defect when it
    quotes or restates the seeded value. That can miss a finding that describes
    the defect without naming it, so the detection rate is a lower bound rather
    than an exact count, and the report says so.
    """
    for path in sorted(reviews_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        reviewer = str(data.get("reviewer") or path.stem)
        for finding in data.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            haystack = finding_haystack(finding)
            for mutation in mutations:
                if mutation.token and mutation.token in haystack:
                    mutation.detected_by.append(reviewer)
                    mutation.matched_findings.append(f"{reviewer}:{finding.get('id')}")
    return mutations


def unmatched_finding_count(reviews_dir: Path, mutations: list[Mutation]) -> int:
    matched = {ref for mutation in mutations for ref in mutation.matched_findings}
    total = 0
    for path in sorted(reviews_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        reviewer = str(data.get("reviewer") or path.stem)
        for finding in data.get("findings") or []:
            if isinstance(finding, dict) and f"{reviewer}:{finding.get('id')}" not in matched:
                total += 1
    return total


def render_report(mutations: list[Mutation], unmatched: int, paper_id: str) -> str:
    detected = [m for m in mutations if m.detected]
    lines = [
        "# Calibration",
        "",
        f"Seeded-defect detection for `{paper_id}`.",
        "",
        f"**{len(detected)} of {len(mutations)} seeded defects were detected.**",
        "",
        "| Defect | Class | Expected auditor | Detected | Found by |",
        "|---|---|---|---|---|",
    ]
    for mutation in mutations:
        found = ", ".join(sorted(set(mutation.detected_by))) or "-"
        lines.append(
            f"| {mutation.defect_id} | {mutation.defect_class} | {mutation.target_auditor} "
            f"| {'yes' if mutation.detected else '**no**'} | {found} |"
        )
    lines += [
        "",
        "## What this measures",
        "",
        "Recall on seeded defects of the classes above, in this manuscript, on "
        "this run. Each defect was written into a copy of the parsed artifacts "
        "and the auditors whose remit covers it were run against that copy.",
        "",
        "## What it does not measure",
        "",
        f"Not a false-positive rate. {unmatched} finding(s) matched no seeded "
        "defect, which is expected and not an error: the manuscript has its own "
        "real defects, and those are what the review is for.",
        "",
        "Not general reliability. Detection of a contradicted number says "
        "nothing about whether an unstated identification assumption or a "
        "misdescribed source would be caught; those classes were not seeded.",
        "",
        "Detection is also a lower bound. A finding counts only when it quotes "
        "or restates the seeded value, so one that describes the defect without "
        "naming it is scored as a miss.",
        "",
    ]
    if len(detected) < len(mutations):
        lines += [
            "## Reading a miss",
            "",
            "A missed defect means this run would not have told you about a real "
            "one of the same class. Treat silence in that class as unmeasured "
            "rather than clean.",
            "",
        ]
    return "\n".join(lines)


def prepare_calibration_parse(source_parsed: Path, target_parsed: Path, *, seed: int = DEFAULT_SEED) -> list[Mutation]:
    """Copy a completed parse and seed defects into the copy."""
    if target_parsed.exists():
        shutil.rmtree(target_parsed)
    target_parsed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_parsed, target_parsed)
    return seed_defects(target_parsed, seed=seed)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--parsed-dir", required=True, help="A completed parse to copy and seed.")
    parser.add_argument("--target-dir", required=True, help="Where the seeded copy is written.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    mutations = prepare_calibration_parse(Path(args.parsed_dir), Path(args.target_dir), seed=args.seed)
    for mutation in mutations:
        print(f"[seed] {mutation.defect_id} {mutation.defect_class} -> {mutation.target_auditor}")
    print(f"[seed] {len(mutations)} defect(s) written into {args.target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
