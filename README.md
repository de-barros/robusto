# robusto

A reproducible multi-agent reviewer for academic economics papers, running on
Claude Code.

robusto is a port of [Ingar30/reviewer](https://github.com/Ingar30/reviewer)
from the Codex CLI to `claude -p`, plus one additional reviewer. The parsing,
routing, validation, normalisation and editor assembly are that project's work.
See [NOTICE](NOTICE) for what came from where.

## What it does

For each paper:

1. parses the PDF locally into source-faithful text, coordinates, page images,
   tables, figures, citations and cross-references
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

Put a PDF in `inputs/` (git-ignored), then:

```bash
python scripts/review_paper.py --pdf "inputs/my-paper.pdf"
```

The report lands at `outputs/my-paper/report.md`. Intermediate artifacts,
prompts, logs, reviewer outputs, routing decisions and the editor bundle are
written under `work/my-paper/`.

Override the model with `--model`; the default is in `config/defaults.toml`.

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

The port is covered by 134 passing unit tests, including the upstream suite and
new tests for the schema-contract layer.

**An end-to-end review has not been run in this repository.** The pipeline
logic is upstream's and is exercised by its tests, but nobody has yet put a PDF
through robusto and read the resulting report. Treat the first run as a trial,
and check `work/<paper_id>/` if a reviewer fails: raw model output is kept
beside every target file precisely so a failure stays inspectable.

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
