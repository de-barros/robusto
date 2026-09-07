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
import os
import re
import shutil
import sys
from pathlib import Path

CLAUDE_CANDIDATES = ("claude.cmd", "claude.exe", "claude")

#: Directories the official installers write to, searched when PATH does not
#: carry the CLI. The native installer drops the binary in ~/.local/bin without
#: necessarily adding that directory to the persisted shell PATH, so
#: `shutil.which` alone reports "not installed" for a working install.
CLAUDE_FALLBACK_DIRS = (
    "~/.local/bin",
    "~/.claude/local",
    "~/AppData/Roaming/npm",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)

#: Point this at a specific binary when several are installed, or when the CLI
#: lives somewhere none of the above cover.
CLAUDE_BIN_ENV = "ROBUSTO_CLAUDE_BIN"

#: The Claude desktop app exports the CLI binary it runs. Authoritative when
#: present: it is the same build the host uses, at whatever version the app
#: last updated itself to, which no guess at an install directory can track.
EXECPATH_ENV = "CLAUDE_CODE_EXECPATH"

#: Which process answers a model call. ``claude`` is the real CLI. ``mock`` is
#: scripts/mock_claude.py, which answers from the prompt alone and spends no
#: tokens, so the whole pipeline after the model call can be exercised on a
#: real manuscript before any reviewer runs for real, and in CI with no login.
BACKEND_ENV = "ROBUSTO_BACKEND"
BACKENDS = ("claude", "mock")


def active_backend() -> str:
    value = (os.environ.get(BACKEND_ENV) or "claude").strip().lower()
    if value not in BACKENDS:
        raise BackendUnavailable(
            f"{BACKEND_ENV}={value!r} is not one of: {', '.join(BACKENDS)}"
        )
    return value

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
    """Absolute path to the Claude Code CLI, or raise.

    Three places, in order: the ``ROBUSTO_CLAUDE_BIN`` override, PATH, then the
    directories the official installers use. The last matters because PATH is
    not a reliable proxy for installation. On Windows the native installer
    writes ``claude.exe`` to ``~/.local/bin`` and leaves the persisted user PATH
    alone, so a PATH-only lookup calls a working install missing.
    """
    override = os.environ.get(CLAUDE_BIN_ENV)
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return str(path)
        raise BackendUnavailable(
            f"{CLAUDE_BIN_ENV} is set to {override}, which is not a file."
        )

    execpath = os.environ.get(EXECPATH_ENV)
    if execpath and Path(execpath).is_file():
        return execpath

    for candidate in CLAUDE_CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    for directory in CLAUDE_FALLBACK_DIRS:
        base = Path(directory).expanduser()
        for candidate in CLAUDE_CANDIDATES:
            path = base / candidate
            if path.is_file():
                return str(path)

    raise BackendUnavailable(
        "Could not find the Claude Code CLI on PATH or in the usual install "
        "directories. Install it (https://claude.com/claude-code), or set "
        f"{CLAUDE_BIN_ENV} to the full path of the binary."
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
    if active_backend() == "mock":
        return [sys.executable, str(Path(__file__).resolve().with_name("mock_claude.py"))]
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


#: Environment variables that redirect the CLI away from the subscription login.
#: These matter more here than in an interactive session: reviewers run as
#: separate `claude -p` processes and inherit this environment, so a gateway
#: configured for interactive use silently captures the whole panel as well. A
#: full run is twenty-odd model calls, so the time to say this is before the
#: run, not on the invoice.
GATEWAY_ENV_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


#: The stock endpoint. Set explicitly to this, ``ANTHROPIC_BASE_URL`` diverts
#: nothing, so warning about it is a false positive, and a check that cries wolf
#: on the default is one the reader learns to skip past.
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

#: Values that mean "off" for the provider switches, which are read as flags.
FALSE_VALUES = ("", "0", "false", "no", "off")


def _diverts(name: str, value: str) -> bool:
    """Does this variable actually send calls somewhere other than the default?"""
    cleaned = value.strip()
    if not cleaned:
        return False
    if name == "ANTHROPIC_BASE_URL":
        return cleaned.rstrip("/").lower() != DEFAULT_ANTHROPIC_BASE_URL
    if name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        return cleaned.lower() not in FALSE_VALUES
    # Credentials: any value at all replaces the subscription login.
    return True


def billing_route_warning() -> str | None:
    """Name the variables diverting model calls off the subscription, if any.

    Presence is not diversion. ``ANTHROPIC_BASE_URL`` set to Anthropic's own
    endpoint changes nothing, and flagging it trains the reader to ignore the
    line that matters.
    """
    present = [
        name
        for name in GATEWAY_ENV_VARS
        if _diverts(name, os.environ.get(name) or "")
    ]
    if not present:
        return None
    return (
        "Model calls are routed away from your Claude subscription by "
        + ", ".join(present)
        + ". Every reviewer in the panel will bill to that endpoint instead. "
        "Unset them to use the subscription."
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


#: A nonce the probe asks for and then requires back. Distinctive enough that it
#: cannot appear by chance in a refusal, an error banner, or a usage message.
PROBE_TOKEN = "robusto-probe-ok"
PROBE_PROMPT = f"Reply with exactly this and nothing else: {PROBE_TOKEN}"


def probe_authentication(timeout_seconds: float = 60.0) -> str | None:
    """Ask the CLI to echo a nonce. Returns a hint on failure, None when healthy.

    Presence on PATH says nothing about whether a session is valid, and an
    unusable one is otherwise discovered only after the deterministic parse has
    run and the first reviewer has been launched.

    The check is positive, not a blocklist. An unauthenticated `claude -p` exits
    0 and prints its refusal to stdout, so exit status proves nothing, and
    matching known refusal wording works only until the wording changes. If that
    happened, the refusal would flow downstream as though it were reviewer output
    and surface as a puzzling schema failure. Requiring the nonce back means any
    reply that is not a working model response fails, whatever it says and
    whatever it exits.
    """
    import subprocess

    try:
        command = claude_exec_command()
    except BackendUnavailable as exc:
        return str(exc)

    # Ask the cheap question first. A CLI holding no credentials cannot answer
    # the probe, and spending a model call to discover that yields a worse
    # message than `claude auth status` gives away for free.
    login = cli_login_hint()
    if login:
        return login

    try:
        completed = subprocess.run(
            command,
            input=PROBE_PROMPT,
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

    # Named diagnoses first, because they say what to do about it.
    hint = auth_failure_hint(combined)
    if hint:
        # A never-signed-in CLI reports an expired session, so prefer the
        # status-backed explanation whenever the status agrees.
        return cli_login_hint() or hint
    if completed.returncode != 0:
        first = next((l.strip() for l in combined.splitlines() if l.strip()), "")
        return f"The Claude Code CLI exited {completed.returncode}: {first[:160]}"

    # Then the catch-all: exit 0, no recognised signature, and still no answer.
    if PROBE_TOKEN not in combined.lower():
        first = next((l.strip() for l in combined.splitlines() if l.strip()), "")
        return (
            "The Claude Code CLI ran but did not answer the probe, so it cannot "
            "serve a review run. It exited 0 and said: "
            f"{first[:160] or '(nothing)'} "
            "Run `claude` once interactively and complete the login, then retry."
        )
    return None


def cli_auth_status(timeout_seconds: float = 30.0) -> dict | None:
    """`claude auth status` as a dict, or None when it cannot be read.

    Free and instant: no model call, no tokens. It answers the question that
    actually matters, and that nothing else here could answer cheaply, namely
    whether the CLI itself holds credentials.
    """
    import subprocess

    try:
        command = [claude_command(), "auth", "status"]
    except BackendUnavailable:
        return None
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    extracted = extract_json_object(completed.stdout or "")
    if extracted is None:
        return None
    try:
        payload = json.loads(extracted)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def cli_login_hint(status: dict | None = None) -> str | None:
    """Say plainly when the CLI holds no credentials, and how to fix it.

    Worth its own message because the failure is easy to misread. A CLI that
    was never signed in reports "OAuth session expired and could not be
    refreshed" and exits 0, which reads as a lapsed login rather than an
    absent one, and sends you looking for a session to restore that never
    existed.
    """
    status = cli_auth_status() if status is None else status
    if status is None or status.get("loggedIn"):
        return None
    lines = [
        "The Claude Code CLI is not signed in; `claude auth status` reports "
        'loggedIn: false, authMethod: "none". Sign it in once with:',
        "",
        "    claude auth login --claudeai",
        "",
        "Run that in a real terminal window you can type into. The login blocks "
        "on an interactive browser handoff, so it cannot be done through a "
        "Claude Code tool call, and asking an agent to run it will hang or "
        "bounce with \"Please run /login\".",
    ]
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") or os.environ.get("CLAUDECODE"):
        lines.append(
            "Being signed in to the Claude desktop app does not sign in the CLI. "
            "The app keeps its OAuth session inside its own process and refreshes "
            "it there, so it never populates the CLI's credential store. "
            "Reviewers run as separate `claude -p` processes and cannot reach the "
            "app's session, so they need this one-time login. It uses the same "
            "subscription and bills the same way."
        )
    return "\n".join(lines)


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
