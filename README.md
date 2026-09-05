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

### Finding the CLI

The binary is looked for in three places, in order: the `ROBUSTO_CLAUDE_BIN`
environment variable, `PATH`, then the directories the official installers use
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

The report lands at `outputs/<paper_id>/report.md`. Intermediate artifacts,
prompts, logs, reviewer outputs, routing decisions and the editor bundle are
written under `work/<paper_id>/`, and `manifest.json` there records which mode
ran, with source mode listing its own limitations explicitly.

Override the model with `--model`; the default is in `config/defaults.toml`.

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

The port is covered by 154 passing unit tests: the upstream suite, plus new
coverage of the schema-contract layer, the LaTeX source front-end, and the
authentication diagnosis.

**No full end-to-end review has completed yet.** A first smoke run got as far
as the deterministic parse and prompt rendering, both of which worked, then
stopped at the preflight auditor because the CLI's token had expired. That
failure is what prompted the authentication probe above. The 21-auditor panel,
the schema-contract retry and the editor assembly remain unexercised against a
real paper.

Treat the first successful run as a trial. If a reviewer fails, look in
`work/<paper_id>/`: raw model output is kept beside every target file precisely
so a failure stays inspectable.

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
