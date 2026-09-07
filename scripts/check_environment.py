from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_backend import (  # noqa: E402
    BackendUnavailable,
    active_backend,
    billing_route_warning,
    claude_command,
    cli_auth_status,
    cli_login_hint,
    probe_authentication,
)


REQUIRED_MODULES = [
    "fitz",
    "pdfplumber",
    "pandas",
    "jsonschema",
    "tabulate",
]

REQUIRED_PATHS = [
    "config/reviewers.json",
    "schemas/reviewer_output.schema.json",
    "schemas/reviewer_selection.schema.json",
    "prompts/templates/editor_report.txt",
    "prompts/templates/reviewer_contract.txt",
    "scripts/pipeline_paths.py",
    "scripts/review_paper.py",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def missing_modules() -> list[str]:
    return [module for module in REQUIRED_MODULES if importlib.util.find_spec(module) is None]


def missing_paths(root: Path) -> list[str]:
    return [path for path in REQUIRED_PATHS if not (root / path).exists()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that the pipeline can run.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Skip the live authentication probe. Checks presence only, which is "
            "what CI wants and what a human almost never does."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("claude", "mock"),
        default=None,
        help="Check the mock backend instead of the real CLI. See review_paper.py --backend.",
    )
    args = parser.parse_args()
    if args.backend:
        os.environ["ROBUSTO_BACKEND"] = args.backend
    root = repo_root()
    failures: list[str] = []

    modules = missing_modules()
    if modules:
        failures.append("Missing Python modules: " + ", ".join(modules))

    paths = missing_paths(root)
    if paths:
        failures.append("Missing project files: " + ", ".join(paths))

    backend = active_backend()
    if backend == "mock":
        print("[check] backend: mock (synthetic replies; no model calls, no tokens)")
        if not args.offline:
            hint = probe_authentication()
            if hint:
                failures.append(hint)
    else:
        # The same resolver the pipeline uses, so a CLI found only in an
        # installer directory is not reported missing here and found later.
        try:
            claude_command()
        except BackendUnavailable as exc:
            failures.append(str(exc))
        else:
            # Free, instant, and the single most common blocker: the CLI keeps
            # credentials separately from the desktop app, so being signed in to
            # the app proves nothing about the subprocesses reviewers run in.
            status = cli_auth_status()
            login = cli_login_hint(status)
            if login:
                failures.append(login)
            else:
                if status:
                    method = status.get("authMethod") or "unknown"
                    print(f"[check] the CLI is signed in (authMethod: {method}).")
                if not args.offline:
                    # Signed in is still not proof of a usable session.
                    print("[check] probing the Claude Code CLI for a valid session ...")
                    hint = probe_authentication()
                    if hint:
                        failures.append(hint)

        # Not a failure: a gateway is a legitimate choice. But it decides which
        # account pays for the run, so it is never left implicit.
        route = billing_route_warning()
        if route:
            print(f"[warn] {route}", file=sys.stderr)
        else:
            print("[check] model calls will use your Claude Code subscription.")

    if failures:
        for failure in failures:
            print(f"[fail] {failure}", file=sys.stderr)
        print("Run the setup steps in README.md and authenticate Claude Code before reviewing papers.", file=sys.stderr)
        return 1

    print("OK: environment looks ready for the reviewer pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
