---
name: robusto
description: "Run a reproducible multi-agent audit of an academic economics paper PDF. Parses the PDF deterministically, routes 21 specialised auditors (numerical, identification, robustness, sample construction, power and multiple testing, data availability, devil's advocate, and more), validates every result against a schema, and assembles an editor's report. Use when asked to review, referee, audit, or stress-test a paper, manuscript, working paper, or preprint before submission. Do NOT use for a summary, a proofread, a rewrite, code review, or a non-academic document."
---

# robusto

A panel audit of an economics paper, ending in one report. This is not a
reading and not a critique: each auditor files findings against a schema, every
finding names the artifact it rests on, and a reviewer that cannot verify
something is required to say so rather than guess.

## Before starting

Confirm three things, and stop if any fails.

1. **The PDF exists.** A bare filename means `inputs/<name>`. A repo-relative
   or absolute path is used as given.
2. **Disclosure permits it.** Parsed manuscript text goes to Anthropic, and
   search-enabled auditors send derived queries to web search. If the paper is
   confidential, unpublished under embargo, or covered by a data agreement that
   forbids third-party transmission, say so and stop. A public working paper is
   normally fine; ask if it is unclear.
3. **The environment is ready.** Run `python scripts/check_environment.py`. It
   checks the Claude Code CLI and the Python dependencies.

## Running it

```bash
python scripts/review_paper.py --pdf "inputs/<paper>.pdf"
```

Add `--paper-id <id>` when the filename is not the identifier you want.
Add `--model <id>` to override `config/defaults.toml`.

This takes a long time and runs many model calls. Say so before starting, and
do not begin a run the user has not asked for.

## What comes back

- `outputs/<paper_id>/report.md` — the editor's report, the deliverable
- `work/<paper_id>/reviews/` — one JSON file per auditor
- `work/<paper_id>/reviews/*.raw.txt` — raw model output, kept for inspection
- `work/<paper_id>/logs/` — stdout and stderr per stage
- `work/<paper_id>/parsed/` — the deterministic parse

## When something fails

A failed auditor is usually a schema-contract failure, not a crash. Claude Code
has no provider-side structured output, so the schema travels in the prompt and
is enforced afterwards; a reviewer that drifts is asked once more with the
errors quoted, then fails.

- Read `work/<paper_id>/reviews/<auditor>.json.raw.txt` for what the model
  actually said.
- Read `work/<paper_id>/logs/<auditor>.stderr.log` for the validation errors.
- One auditor failing does not invalidate the rest; the editor works from what
  validated. Say which auditor dropped out when reporting.

Rerun the editor alone, without redoing the panel:

```bash
python scripts/refresh_editor.py --paper-id <id> --run-editor
```

## Reporting to the user

Lead with what the panel found that matters, not with pipeline mechanics.
Give the devil's advocate its own paragraph: it is the only auditor that
argues rather than checks, and its ranked objections are the closest thing to
what a referee will actually write.

Two failure modes to avoid. Do not summarise the report back at length; point
to it and draw out what changes a decision. And do not present a `cannot_verify`
as a defect, since it means the artifacts did not support a check, which is
often a parsing limit rather than a flaw in the paper.

## Boundaries

This skill audits a finished PDF. For drafting prose, rebuilding exhibits,
checking a bibliography, or auditing analysis code, use the tools suited to
those; running a full panel to answer a narrow question wastes an hour.
