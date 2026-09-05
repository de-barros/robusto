"""Claude Code transport for the reviewer pipeline.

The upstream pipeline (Ingar30/reviewer, MIT) talks to the Codex CLI, which
supplies three things the rest of the pipeline depends on:

    codex exec --output-schema S --output-last-message O -

    1. a prompt read from stdin
    2. the final assistant message written to a named file
    3. output constrained to a JSON Schema, enforced by the provider

`claude -p` gives us the first and, with a redirect, the second. It has no
equivalent of `--output-schema`. This module supplies the third in userland:
the schema is inlined into the prompt under an explicit contract, the model's
stdout is then parsed and validated against the same schema file, and callers
retry with the validation error appended when a reviewer drifts.

That trade is the one real cost of the port. Provider-side constrained
decoding cannot fail; a prompt contract can. Everything downstream, including
`validate_review_json.py`, still refuses malformed output, so a drifting
reviewer surfaces as a failed run rather than as a silently malformed report.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

CLAUDE_CANDIDATES = ("claude.cmd", "claude.exe", "claude")

#: Tools a reviewer may use. The pipeline is deliberately read-only: reviewers
#: reason over already-parsed artifacts and must not edit the repository.
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
SEARCH_TOOL = "WebSearch"

SCHEMA_CONTRACT = """
# Output contract

Return **one** JSON object and nothing else. No prose before or after it, no
explanation, no markdown code fence. The very first character of your reply
must be `{{` and the very last must be `}}`.

The object must validate against this JSON Schema:

```json
{schema}
```

Rules that the schema cannot express:

- Never invent a value to satisfy a required field. If the parsed source does
  not support a claim, use the schema's `cannot_verify` route instead.
- Quote evidence verbatim from the parsed artifacts. Do not paraphrase a
  number, a sign, a label, or a table cell.
- If the artifacts are too damaged to judge, say so through the schema rather
  than guessing.
""".strip()

RETRY_PREFIX = """
Your previous reply did not satisfy the output contract.

{errors}

Return the corrected JSON object only.
""".strip()


class BackendUnavailable(RuntimeError):
    """Raised when the Claude Code CLI cannot be located."""


def claude_command() -> str:
    """Absolute path to the Claude Code CLI, or raise."""
    for candidate in CLAUDE_CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise BackendUnavailable(
        "Could not find the Claude Code CLI on PATH. Install it "
        "(https://claude.com/claude-code) or add `claude` to PATH."
    )


def claude_exec_command(
    *,
    model: str | None = None,
    search: bool = False,
    allow_read: bool = True,
) -> list[str]:
    """Build the headless Claude Code invocation.

    Mirrors upstream's ``codex_exec_command``. ``reasoning_effort`` has no
    counterpart and is deliberately dropped rather than faked.
    """
    command = [claude_command(), "-p"]
    if model:
        command.extend(["--model", model])
    tools: list[str] = list(READ_ONLY_TOOLS) if allow_read else []
    if search:
        tools.append(SEARCH_TOOL)
    if tools:
        command.extend(["--allowed-tools", ",".join(tools)])
    return command


def structured_prompt(prompt_text: str, schema_path: Path) -> str:
    """Append the schema contract that replaces ``--output-schema``."""
    schema = json.dumps(
        json.loads(schema_path.read_text(encoding="utf-8")), indent=2
    )
    contract = SCHEMA_CONTRACT.replace("{schema}", schema)
    return f"{prompt_text.rstrip()}\n\n---\n\n{contract}\n"


def retry_prompt(prompt_text: str, schema_path: Path, errors: list[str]) -> str:
    """Re-ask, quoting the validation failures."""
    rendered = "\n".join(f"- {e}" for e in errors[:12])
    return (
        structured_prompt(prompt_text, schema_path)
        + "\n\n---\n\n"
        + RETRY_PREFIX.replace("{errors}", rendered)
        + "\n"
    )


def extract_json_object(raw: str) -> str | None:
    """Recover the JSON object from a model reply.

    Tolerates a fenced block or stray prose either side, which the schema
    contract asks the model to omit but cannot guarantee. Returns ``None``
    when no balanced object is present.
    """
    if not raw or not raw.strip():
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        candidate = fenced.group(1)
        if _is_json(candidate):
            return candidate

    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start : i + 1]
                    if _is_json(candidate):
                        return candidate
                    break
        start = raw.find("{", start + 1)
    return None


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


#: Signatures the CLI emits when it is installed but cannot authenticate. Worth
#: detecting explicitly: the process exits non-zero before writing anything the
#: schema layer could inspect, so without this the failure reaches the user as
#: an opaque "reviewer run failed (1)".
#: Every phrase here must be specific to authentication. A bare "401" is not:
#: a manuscript reporting 401 students, or a 401(k), would be diagnosed as a
#: login failure, so the status code only counts next to an error label.
AUTH_SIGNATURES = (
    "oauth access token has expired",
    "failed to authenticate",
    "re-authenticate",
    "invalid api key",
    "authentication_error",
    "api error: 401",
    "error 401",
    "http 401",
    "not logged in",
    "please run /login",
)


def auth_failure_hint(text: str | None) -> str | None:
    """Return a readable diagnosis when `text` looks like an auth failure."""
    if not text:
        return None
    lowered = text.lower()
    if not any(signature in lowered for signature in AUTH_SIGNATURES):
        return None
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return (
        "The Claude Code CLI is installed but not authenticated. "
        f"It reported: {first[:160]} "
        "Run `claude` once interactively and complete the login, then retry."
    )


def probe_authentication(timeout_seconds: float = 60.0) -> str | None:
    """Ask the CLI for one token. Returns a hint on failure, None when healthy.

    Presence on PATH says nothing about whether a token is still valid, and an
    expired one is otherwise discovered only after the deterministic parse has
    run and the first reviewer has been launched.
    """
    import subprocess

    try:
        command = claude_exec_command()
    except BackendUnavailable as exc:
        return str(exc)
    try:
        completed = subprocess.run(
            command,
            input="Reply with the single character: k",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"The Claude Code CLI did not respond within {timeout_seconds:.0f}s."
    except OSError as exc:
        return f"Could not run the Claude Code CLI: {exc}"

    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    hint = auth_failure_hint(combined)
    if hint:
        return hint
    if completed.returncode != 0:
        first = next((l.strip() for l in combined.splitlines() if l.strip()), "")
        return f"The Claude Code CLI exited {completed.returncode}: {first[:160]}"
    return None


def schema_errors(payload: dict, schema_path: Path) -> list[str]:
    """Validate against the schema file, returning readable messages."""
    from jsonschema import Draft202012Validator

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    messages = []
    for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in error.path) or "(root)"
        messages.append(f"{location}: {error.message}")
    return messages


def finalize_structured_output(
    raw_path: Path, output_path: Path, schema_path: Path
) -> tuple[bool, list[str]]:
    """Turn captured stdout into the JSON file the pipeline expects.

    Returns ``(ok, errors)``. On success ``output_path`` holds pretty-printed
    JSON that validates against ``schema_path``; the pipeline's own
    ``validate_review_json.py`` still runs afterwards and applies the semantic
    rules this function does not (provenance, cannot-verify discipline).
    """
    if not raw_path.exists():
        return False, [f"no output captured at {raw_path}"]

    raw = raw_path.read_text(encoding="utf-8", errors="replace")
    extracted = extract_json_object(raw)
    if extracted is None:
        head = raw.strip()[:200].replace("\n", " ")
        return False, [f"reply contained no parseable JSON object; begins: {head!r}"]

    payload = json.loads(extracted)
    errors = schema_errors(payload, schema_path)
    if errors:
        return False, errors

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return True, []
