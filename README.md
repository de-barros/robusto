# robusto

A reproducible multi-agent reviewer for academic economics papers, running on
Claude Code.

robusto reads a manuscript, runs a panel of specialised auditors over it, and
assembles one editor's report. Every finding is validated against a schema and
names the artifact it rests on; a reviewer that cannot verify something is
required to say so rather than guess.

It is a port of [Ingar30/reviewer](https://github.com/Ingar30/reviewer) from the
Codex CLI to `claude -p`, plus one additional reviewer. The parsing, routing,
validation, normalisation and editor assembly are that project's work. See
[NOTICE](NOTICE) for what came from where.

## What it does

1. Builds source-faithful artifacts from the manuscript: text, sections,
   tables, figures, numbers, citations and cross-references, plus page
   coordinates and images when working from a PDF.
2. Runs a parser-quality preflight before any substantive review, so a bad
   parse is caught rather than silently reviewed.
3. Selects every reviewer whose remit is plausibly relevant, falling back to
   the full roster when applicability is uncertain.
4. Runs the selected auditors in parallel batches and validates every result.
5. Normalises duplicate findings without discarding source evidence.
6. Asks an editor to lead with concrete correctness problems, then broader
   positioning.
7. Writes the report to `outputs/<paper_id>/report.md`.

Both parsers are deterministic and local. They use no hosted OCR, no document
service and no model repair layer, and they never invent a missing sign, value,
label, cell or formula. Artifacts that cannot be recovered safely are flagged,
so a reviewer falls back to other evidence or answers `cannot_verify`.

The review itself is not local. Parsed manuscript text goes to Anthropic
through the Claude Code CLI, and search-enabled reviewers send derived queries
to web search. **Do not review a confidential manuscript unless its disclosure
terms permit that.**

## The reviewers

One preflight auditor checks parse quality. Twenty substantive auditors then
cover cross-references, source consistency, numerical checks, claim-evidence
links, literature, references, grammar, identification, robustness, sample
construction, abstract-conclusion consistency, limitations and external
validity, model equations, theory logic, data availability and replication,
institutional context, power and multiple testing, design and randomisation,
and economic magnitude.

Nineteen of the twenty are inherited from upstream. One is added here:

**`devils_advocate_auditor`** builds the case *against* the paper. Every other
reviewer hunts a class of defect; this one argues, as a hostile but competent
referee would, and ranks objections by how likely they are to sink the paper.
It must name the passage each objection attacks and say what evidence would
answer it, so it produces arguments rather than doubts. A clean result is
reported as clean and is not padded.

## Related tools

Several open tools review academic work, and they are not substitutes for one
another. They audit different objects, so which one fits depends on what you
need checked.

- **[Ingar30/reviewer](https://github.com/Ingar30/reviewer)** (MIT) — the
  project robusto is ported from. Most of what robusto does is its design.
- **[referee2](https://github.com/scunning1975/MixtapeTools)**, in Scott
  Cunningham's MixtapeTools — audits the code behind a paper.
- **[academic-paper-reviewer](https://github.com/Imbad0202/academic-research-skills)**
  (CC BY-NC) — simulates a journal peer-review panel.

| | robusto | referee2 | academic-paper-reviewer |
|---|---|---|---|
| Audits | the manuscript text | the code that produced the numbers | the paper as a scholarly contribution |
| Runs your analysis | no | **yes** — replicates the pipeline in two further languages among R, Stata and Python |  no |
| Input | PDF or LaTeX source, deterministically parsed | the project repository | the paper |
| Structure | 20 auditors in parallel, schema-validated findings | one auditor, five sequential audits | five review seats, plus a field analyst and an editorial synthesiser |
| Venue | not modelled | not modelled | a journal-fit review seat, informed by tiered journal lists |
| Verdict | none, by design | Accept / Minor / Major Revisions / Reject | narrative judgements rather than numeric scores, plus an editorial decision |
| Scope | economics | empirical projects; also slide decks | field-general |
| Distinctive | seeded-defect calibration, mock backend | cross-language replication | re-review of an R&R, Socratic mode, self-calibration |

What each cannot do matters as much:

- **robusto never executes anything.** It checks whether the manuscript is
  internally consistent and adequately supported, not whether the numbers are
  correct. A pipeline bug that reports a wrong value consistently throughout is
  invisible to it.
- **referee2 needs the code** and a working R, Stata and Python toolchain, so it
  does not apply to a paper you only have as a PDF.
- **academic-paper-reviewer reads the paper directly**, without a deterministic
  parse layer, and returns a judgment rather than a list of corrections.

They compose, and the order is worth thinking about: verifying the pipeline
before the manuscript avoids carefully auditing prose built on wrong numbers.

robusto's calibration below is adapted from academic-paper-reviewer's
calibration mode. The idea, that a reviewer's own error profile should be
measured rather than assumed, is theirs; the metric differs because the two
tools emit different things.

This comparison was checked against each project's repository on 2026-09-07.
All three are moving targets, so treat it as a snapshot and check upstream if a
detail matters to a decision.

## Requirements

Python 3.12+, the [Claude Code CLI](https://claude.com/claude-code), and web
search available to it.

```bash
git clone https://github.com/de-barros/robusto.git
cd robusto
bash setup.sh && source .venv/bin/activate      # or .\setup.ps1 on Windows
python -m unittest discover -s tests -t .
python scripts/check_environment.py
```

### Signing in

Reviewers run as separate `claude -p` processes, so the CLI needs its own
credentials:

```bash
claude auth login --claudeai
```

Once per machine, in a real terminal, since the login blocks on a browser
handoff. Verify with `claude auth status`.

**Being signed in to the Claude desktop app does not sign in the CLI.** The app
keeps its OAuth session in its own process and never writes the CLI's
credential store. A CLI that was never signed in reports `OAuth session expired
and could not be refreshed` and exits 0, which reads like a lapsed session
rather than an absent one, so check `claude auth status` before believing it.
`check_environment.py` checks this first, and it is free and instant.

### Finding the CLI

The binary is looked for in four places, in order: `ROBUSTO_CLAUDE_BIN`,
`CLAUDE_CODE_EXECPATH` (exported by the desktop app), `PATH`, then the usual
install directories (`~/.local/bin`, `~/.claude/local`, `%APPDATA%/npm`,
`/usr/local/bin`, `/opt/homebrew/bin`). PATH alone is not a reliable proxy for
installation, since some installers do not modify it. Set `ROBUSTO_CLAUDE_BIN`
to a full path when several versions are installed.

### Which account pays

Reviewers inherit the environment of whatever launched the pipeline. If that
environment sets `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_API_KEY`, `ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_USE_BEDROCK` or
`CLAUDE_CODE_USE_VERTEX`, every call bills to that endpoint rather than to your
Claude subscription, and a gateway configured for interactive use captures the
whole run silently.

`check_environment.py` and the run header name any such variable they find.
Neither treats it as an error, since a gateway is a legitimate choice; the point
is that a long run should never be ambiguous about which account it spends
from. Claude Code reads these from `~/.claude/settings.json` as well as the
shell.

## Three ways in

Exactly one is required.

```bash
# a finished PDF
python scripts/review_paper.py --pdf "inputs/my-paper.pdf"

# compile the manuscript first, then review what comes out
python scripts/review_paper.py --build "path/to/manuscript.tex"

# review the LaTeX source directly, without typesetting
python scripts/review_paper.py --source "path/to/manuscript.tex"
```

They are not interchangeable, and the difference is not convenience.

| | `--pdf` / `--build` | `--source` |
|---|---|---|
| numbers | recovered from rendered glyphs | exact, from the generated snippets |
| cross-references | inferred from "Table 3" in the text | exact, from the label and reference commands |
| citations | parsed from rendered markers | exact, the keys themselves |
| reference list | parsed from the typeset bibliography | exact, from the .bib |
| table bodies | reconstructed from layout | the snippet, verbatim |
| **layout, overflow, page budget** | **visible** | **invisible** |
| **figure legibility after reduction** | **visible** | **invisible** |
| page numbers in findings | present | null; findings anchor on section and quoted text |

Use `--source` when the manuscript lives in a repository and you care whether
the numbers, references and claims hang together. Use `--build` before
submitting, when you care what the referee actually receives: a table running
off the page, or axis labels below a journal's point floor, exist only after
typesetting.

`--build` needs `latexmk` on PATH. `--source` takes a root .tex file, or a
directory containing exactly one file with a documentclass.

### What source mode does with generated values

Economics manuscripts routinely inject results from a generated file of macro
definitions, so the prose reads "the coefficient is `$\effect$`" while the
number lives elsewhere. Source mode substitutes those values before anything
reads the text; without it, the prose is number-free and the numerical auditor
has nothing to check, with nothing to indicate the audit was blind.

Precedence follows LaTeX, so a generated definition beats a `\providecommand`
placeholder kept for compiling before results exist. Macros taking arguments,
and macros whose body holds a real command, are left alone as formatting rather
than values. Substitutions are recorded in `parsed/macro_expansions.json`, so a
finding resting on an expanded number can be traced to its definition.

Section titles are extracted with balanced braces, so a `\label{...}` nested
inside `\section{...}` is captured into a `section_label` field instead of
truncating the title.

## Output

The report lands at `outputs/<paper_id>/report.md`. Intermediate artifacts,
prompts, logs, reviewer outputs, routing decisions and the editor bundle are
written under `work/<paper_id>/`, where `parsed/manifest.json` records which
mode ran and lists that mode's limitations explicitly.

Raw model output is kept beside every validated file, so a failed reviewer
stays inspectable.

## A dry run that costs nothing

```bash
python scripts/review_paper.py --backend mock --source "path/to/manuscript.tex" --paper-id smoke
```

`--backend mock` replaces every model call with `scripts/mock_claude.py`, which
answers from the prompt alone with synthetic, schema-valid output. Everything
else runs for real on your manuscript: the parse, prompt rendering, the
preflight gate, routing, all reviewers, the schema contract and its retry,
validation, normalisation, editor assembly and the final-report check. It takes
about fifteen seconds.

Use it to confirm the parse is sound before spending anything, and in CI, where
no CLI is logged in. A mock report cannot be mistaken for a review: it opens
with a banner saying so, every finding names the mock, and `run_manifest.json`
records `"backend": "mock"`.

Two environment variables reach paths a clean run never touches:

```bash
ROBUSTO_MOCK_DRIFT=numerical_auditor   # first reply breaks the contract; the retry must recover
ROBUSTO_MOCK_FAIL=robustness_auditor   # every reply breaks it; the run must stop, naming the reviewer
```

`ROBUSTO_MOCK_FINDINGS=N` sets findings per reviewer and `ROBUSTO_MOCK_SKIP`
makes the selector skip reviewers.

## Starting small on a real paper

A full panel is a long run and many model calls. To test a new manuscript for
the price of one call:

```bash
python scripts/review_paper.py --source "path/to/manuscript.tex" --paper-id first --stop-after preflight
```

That runs the parse and the parser-quality auditor, then stops cleanly, having
put one real reply through the contract, the validator and the gate.
`--stop-after selection` goes one call further and shows which reviewers would
run. Either continues with:

```bash
python scripts/review_paper.py --source "path/to/manuscript.tex" --paper-id first --resume-after-preflight
```

Resume reuses the parsed artifacts and the validated preflight output. The
manifest records where a run stopped.

## Calibration: did this run catch anything?

A review that reports nothing is ambiguous. The paper may be clean, or the
auditors may have missed what is wrong, and the report cannot tell you which.
The second reading is the one that matters.

After the report is written, robusto plants defects it knows about and checks
whether they come back:

```
[calibration] 3 defect(s) seeded, 3 auditor(s) to check them
[calibration] detected 2 of 3 seeded defects
[calibration] MISSED SEED-REF-001 (citation_missing_from_bibliography)
```

The result lands at `outputs/<paper_id>/calibration.md`. Defects are written
into a copy of the parsed artifacts, never the run's own, and only the auditors
whose remit covers a seeded class are run, which keeps this to three or four
extra calls rather than a second panel. Current classes: a prose value
contradicting the table it came from, a cross-reference pointing at a label
that does not exist, and a citation absent from the bibliography.

Turn it off with `--no-calibration`. `--calibration-seed` chooses which targets
are planted; it is fixed by default so repeated runs measure the same thing.

**What it measures.** Recall on seeded defects of those classes, in that
manuscript, on that run.

**What it does not.** It is not a false-positive rate: findings that match no
seeded defect are counted and reported as unmatched, not scored as errors,
because the manuscript has real defects of its own and those are the point. It
does not generalise across classes either, since catching a contradicted
coefficient says nothing about whether an unstated identification assumption
would be caught. And detection is a lower bound, because a finding counts only
when it quotes or restates the seeded value, so one that describes the defect
without naming it is scored as a miss.

A missed defect is the useful output. It means this run would not have told you
about a real defect of that class, so silence there should be read as
unmeasured rather than clean.

## Using it as a Claude Code skill

The repository ships a skill at `.claude/skills/robusto/`, which Claude Code
picks up automatically inside this repo. To reach it from anywhere:

```bash
mkdir -p ~/.claude/skills
cp -r .claude/skills/robusto ~/.claude/skills/
```

Then ask for a review in plain language: *"review inputs/my-paper.pdf with
robusto"*, *"referee the manuscript in paper/main.tex"*, or just *"stress-test
this paper before I submit it"*.

The skill is a thin wrapper, so **the clone still has to exist** with its
virtual environment; the skill finds it rather than replacing it. It looks in
`$ROBUSTO_HOME` first, then `~/Documents/GitHub/robusto`, `~/GitHub/robusto`,
`~/code/robusto`, `~/src/robusto`, `~/robusto`, and finally the working
directory. If your clone lives elsewhere:

```bash
export ROBUSTO_HOME="/path/to/robusto"     # add to ~/.bashrc or ~/.zshrc
```

```powershell
[Environment]::SetEnvironmentVariable('ROBUSTO_HOME', 'C:/path/to/robusto', 'User')
```

Reports are written inside the repo, at `outputs/<paper_id>/report.md`, not in
the directory you were standing in.

## How the port works, and what it costs

Upstream invokes Codex as:

```
codex exec --output-schema S --output-last-message O -
```

which supplies three things: a prompt on stdin, the final message written to a
named file, and **output constrained to a JSON schema, enforced by the
provider**. `claude -p` gives the first, and the second with a redirect. It has
no equivalent of `--output-schema`.

`scripts/claude_backend.py` supplies the third in userland. The schema is
inlined into the prompt under an explicit contract; stdout is then parsed,
validated against the same schema file, and on failure the reviewer is asked
once more with the validation errors quoted back.

**This is the one real cost of the port.** Provider-side constrained decoding
cannot drift; a prompt contract can. The mitigation is that nothing downstream
trusts the model: `finalize_structured_output` refuses anything that does not
validate, `validate_review_json.py` applies the semantic rules on top, and a
reviewer that will not conform fails loudly instead of poisoning the report.

Reviewers run with `Read`, `Grep`, `Glob` and optionally `WebSearch`. No write
tool is ever granted, which the test suite asserts.

`--reasoning-effort` is accepted so existing scripts and habits carry over, but
Claude Code has no such control, so it is ignored rather than translated into
something it does not mean.

## Status

277 passing tests, run on Linux and Windows in CI: the upstream suite, the
schema-contract layer, the LaTeX source front-end, the environment and
authentication checks, the mock backend, and an end-to-end pipeline run on a
fixture manuscript.

The pipeline has completed end to end against a real model. On a 31,600-word
manuscript assembled from 95 included files (56 sections, 25 tables, 9 figures,
no undefined labels), nineteen reviewers ran in parallel batches over about 80
minutes and every one returned schema-valid output on the first attempt. The
retry and fail-loud branches, which a clean run never reaches, are covered by
the mock backend instead.

Calibration is on by default, so a completed run also reports how many
defects it was able to plant and detect. That number is the honest check on
everything above.

This has been exercised on a small number of manuscripts. The prompt contract
holding is an empirical result, not a guarantee, which is why validation is
enforced at three layers downstream and why raw model output is always kept.

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
