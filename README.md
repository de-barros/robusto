# robusto

A reproducible multi-agent reviewer for academic economics papers, running on
Claude Code.

robusto is a port of [Ingar30/reviewer](https://github.com/Ingar30/reviewer)
from the Codex CLI to `claude -p`, plus one additional reviewer. The parsing,
routing, validation, normalisation and editor assembly are that project's work.
See [NOTICE](NOTICE) for what came from where.

## What it does

For each paper:

1. builds source-faithful artifacts from the manuscript: text, tables,
   figures, citations and cross-references, plus coordinates and page images
   when working from a PDF
2. runs a parser-quality preflight before any substantive review
3. selects every reviewer whose remit is plausibly relevant, falling back to
   the full roster when applicability is uncertain
4. runs 21 specialised auditors and validates every structured result
5. normalises clear duplicates without discarding source evidence
6. asks an editor to lead with concrete correctness and auditability problems,
   then broader positioning
7. writes the report to `outputs/<paper_id>/report.md`

The PDF parser is deterministic and local. It uses no hosted OCR, no document
service and no model repair layer, and it never invents a missing sign, value,
label, cell or formula. Unsafe artifacts are flagged so a reviewer can fall
back to page images or answer `cannot_verify`.

The review itself is not local. Parsed manuscript text goes to Anthropic
through the Claude Code CLI, and search-enabled reviewers send derived queries
to web search. **Do not review a confidential manuscript unless its disclosure
terms permit that.**

## The reviewers

Twenty inherited from upstream, covering parser quality, cross-references,
source consistency, numerical checks, claim-evidence links, literature,
references, grammar, identification, robustness, sample construction,
abstract-conclusion consistency, limitations and external validity, model
equations, theory logic, data availability and replication, institutional
context, power and multiple testing, design and randomisation, and economic
magnitude.

One added here:

**`devils_advocate_auditor`** builds the case *against* the paper. Every other
reviewer hunts a class of defect; this one argues, as a hostile but competent
referee would, and ranks objections by how likely they are to sink the paper.
It is required to name the passage each objection attacks and to say what
evidence would answer it, so that it produces arguments rather than doubts. A
clean result is reported as clean and is not padded.

## Quick start

```bash
git clone https://github.com/de-barros/robusto.git
cd robusto
bash setup.sh && source .venv/bin/activate      # or .\setup.ps1 on Windows
python -m unittest discover -s tests -t .
python scripts/check_environment.py
```

You need Python 3.12+, the [Claude Code CLI](https://claude.com/claude-code)
installed and authenticated, and web search available to it.

`check_environment.py` makes one small model call to confirm the CLI can
actually authenticate. Presence on PATH does not imply a valid session, and an
expired token otherwise surfaces only after the deterministic parse has run and
the first reviewer has been launched. Pass `--offline` to skip the probe and
check presence only, which is what CI wants.

### Signing in the CLI

Reviewers run as separate `claude -p` processes, so the CLI needs its own
credentials:

```bash
claude auth login --claudeai
```

Once, on the machine that will run reviews, **in a real terminal window you
can type into**. The login blocks on an interactive browser handoff, so it
cannot be run through a Claude Code tool call or by asking an agent to run it;
that hangs, or bounces with `Please run /login`. It uses your Claude
subscription and bills the same way. Verify with `claude auth status`, which
should report `loggedIn: true`.

**Being signed in to the Claude desktop app does not sign in the CLI.** The app
keeps its OAuth session inside its own process and refreshes it there, so it
never populates the CLI's credential store, and a subprocess cannot reach it.
This is worth stating plainly because the failure is easy to misread: a CLI that
was never signed in reports `OAuth session expired and could not be refreshed`
and exits 0, which reads as a lapsed session rather than an absent one, and
sends you looking for a login to restore that never existed. `check_environment.py`
now asks `claude auth status` first, which is free and instant, and says so
outright.

### Finding the CLI

The binary is looked for in four places, in order: the `ROBUSTO_CLAUDE_BIN`
environment variable, `CLAUDE_CODE_EXECPATH` (which the desktop app exports,
naming the exact build it runs), `PATH`, then the directories the installers use
(`~/.local/bin`, `~/.claude/local`, `%APPDATA%/npm`, `/usr/local/bin`,
`/opt/homebrew/bin`). PATH is not a reliable proxy for installation: the native
Windows installer writes `claude.exe` to `~/.local/bin` and leaves the persisted
user PATH alone, which made a perfectly working install look absent. Set
`ROBUSTO_CLAUDE_BIN` to a full path when several are installed.

### Which account pays

Reviewers run as separate `claude -p` processes and inherit the environment of
whatever launched the pipeline. If that environment sets `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_CUSTOM_HEADERS`,
`CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX`, then every call in the
panel bills to that endpoint rather than to your Claude subscription. A gateway
configured for interactive use captures the whole run silently.

`check_environment.py` and the run header both name any such variable they find.
Neither treats it as an error, since routing through a gateway is a legitimate
choice; the point is that a twenty-call run should never be ambiguous about
which account it is spending from. Note that Claude Code reads these from
`~/.claude/settings.json` at launch as well as from the shell.

## Three ways in

Exactly one of these is required.

```bash
# 1. a finished PDF
python scripts/review_paper.py --pdf "inputs/my-paper.pdf"

# 2. compile the manuscript first, then review what comes out
python scripts/review_paper.py --build "path/to/manuscript.tex"

# 3. review the LaTeX source directly, without typesetting
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

Use `--source` when the manuscript lives in a repository and you care about
whether the numbers, references and claims hang together. Use `--build` before
submitting, when you care about what the referee actually receives: a table
running off the page or axis labels below a journal's point floor exist only
after typesetting, and source mode cannot see them.

`--build` needs `latexmk` on PATH. `--source` takes a root .tex file, or a
directory containing exactly one file with a documentclass.

Source mode substitutes value macros before anything reads the text. Economics
manuscripts routinely inject their results from a generated file of
definitions, so the prose says "the coefficient is $\effect$" and the number
lives elsewhere. Left alone, that reads as number-free prose: the numerical
auditor sees a sentence with nothing in it to check, reports nothing, and
nothing errors to say the audit was blind. Precedence follows LaTeX, so a
generated definition beats the placeholder a manuscript keeps so it still
compiles before results exist; getting that backwards would put "[run
master.do]" into the prose as though it were a finding. Macros taking
arguments and macros whose body holds a real command are left alone, since
those are formatting rather than values. What was substituted is recorded in
`parsed/macro_expansions.json`, so a finding resting on an expanded number can
be traced to the definition it came from.

The report lands at `outputs/<paper_id>/report.md`. Intermediate artifacts,
prompts, logs, reviewer outputs, routing decisions and the editor bundle are
written under `work/<paper_id>/`, and `manifest.json` there records which mode
ran, with source mode listing its own limitations explicitly.

Override the model with `--model`; the default is in `config/defaults.toml`.

## Proving the pipeline without spending a token

```bash
python scripts/review_paper.py --backend mock --source "path/to/manuscript.tex" --paper-id smoke
```

`--backend mock` replaces every model call with `scripts/mock_claude.py`, which
answers from the prompt alone with synthetic, schema-valid output. Everything
else runs for real on your actual manuscript: the deterministic parse, prompt
rendering, the preflight gate, applicability routing, all twenty reviewers in
parallel batches, the schema contract and its bounded retry, semantic
validation, normalisation, editor assembly, and the final-report check. A full
run takes about fifteen seconds and costs nothing.

Use it before a real run to confirm the parse is sound and the machinery holds,
and in CI, where no CLI is logged in. A mock report cannot be mistaken for a
review: it opens with a banner saying so, every finding names the mock, and
`run_manifest.json` records `"backend": "mock"`.

Two knobs reach the paths a clean run never touches:

```bash
ROBUSTO_MOCK_DRIFT=numerical_auditor   # first reply breaks the contract; the retry must recover
ROBUSTO_MOCK_FAIL=robustness_auditor   # every reply breaks it; the run must stop, naming the reviewer
```

`ROBUSTO_MOCK_FINDINGS=N` sets findings per reviewer (default 2) and
`ROBUSTO_MOCK_SKIP=name,name` makes the selector skip optional reviewers.
`check_environment.py --backend mock` confirms the mock answers the probe.

`tests/test_mock_pipeline.py` runs this end to end on a tiny fixture manuscript,
including `--resume-after-preflight` and both misbehaviour paths.

## The first real run, one call at a time

The mock proves everything except the one thing that matters most: whether a
real reviewer honours the schema contract on your paper. That can be settled
for the price of a single model call.

```bash
python scripts/review_paper.py --source "path/to/manuscript.tex" --paper-id first --stop-after preflight
```

`--stop-after preflight` runs the parse and the parser-quality auditor, then
stops cleanly. One reviewer has now answered a real prompt against a real
manuscript and its reply has been through the contract, the validator and the
gate. `--stop-after selection` goes one call further and shows you which of the
twenty reviewers would run. Either way, continue with:

```bash
python scripts/review_paper.py --source "path/to/manuscript.tex" --paper-id first --resume-after-preflight
```

Resume reuses the parsed artifacts and the validated preflight output; only the
selector runs again. The manifest records where a run stopped.

## Using it as a Claude Code skill

The repository ships a skill at `.claude/skills/robusto/`, which Claude Code
picks up automatically when you work inside this repo. To reach it from any
directory, install it for your user:

```bash
mkdir -p ~/.claude/skills
cp -r .claude/skills/robusto ~/.claude/skills/
```

Then ask for a review from anywhere: *"review inputs/my-paper.pdf with
robusto"*, or just *"referee this paper"* with a PDF to hand.

The skill is a thin wrapper, so **the clone still has to exist** with its
virtual environment set up; the skill finds it rather than replacing it. It
looks in `$ROBUSTO_HOME` first, then `~/Documents/GitHub/robusto`,
`~/GitHub/robusto`, `~/code/robusto`, `~/src/robusto`, `~/robusto`, and finally
the working directory. If your clone lives somewhere else, set the variable:

```bash
export ROBUSTO_HOME="/path/to/robusto"     # add to ~/.bashrc or ~/.zshrc
```

```powershell
[Environment]::SetEnvironmentVariable('ROBUSTO_HOME', 'C:/path/to/robusto', 'User')
```

Reports are written inside the repo, at `outputs/<paper_id>/report.md`, not in
whatever directory you were standing in.

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
validate, `validate_review_json.py` then applies the semantic rules on top, and
a reviewer that will not conform fails loudly instead of poisoning the report.

Reviewers run with `Read`, `Grep`, `Glob` and optionally `WebSearch`. No write
tool is ever granted, which is asserted in the test suite.

`--reasoning-effort` is accepted so scripts and habits carry over, but Claude
Code has no such control, so it is ignored rather than translated into
something it does not mean.

## Status

Covered by 246 passing tests: the upstream suite, the schema-contract layer,
the LaTeX source front-end, the authentication and billing diagnosis, the mock
backend, and an end-to-end run of the whole pipeline on a fixture manuscript.

**The pipeline has completed end to end, with the mock backend standing in for
the model.** On a 17,600-word manuscript assembled from 22 included files: the
deterministic parse (27 sections, 12 tables, 4 figures, no undefined labels),
prompt rendering, the preflight gate, applicability routing, all twenty
reviewers in parallel batches, the schema contract with its bounded retry,
semantic validation, normalisation, editor assembly, the final-report check,
and `--resume-after-preflight`. The retry path was exercised by forcing one
reviewer to drift, and the fail-loud path by forcing one to drift twice.

**What has not run is a real model call.** The one guarantee the port gives up,
provider-enforced structured output, is replaced by a prompt contract, and
whether a real reviewer honours that contract on a real paper is precisely what
the mock cannot tell you. The first real run reached the preflight auditor
before the CLI's session failed; that failure produced the authentication probe
and the billing-route check above.

Treat the first successful real run as a trial. If a reviewer fails, look in
`work/<paper_id>/`: raw model output is kept beside every target file precisely
so a failure stays inspectable.

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
