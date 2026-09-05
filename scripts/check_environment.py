from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_backend import probe_authentication  # noqa: E402


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
    args = parser.parse_args()
    root = repo_root()
    failures: list[str] = []

    modules = missing_modules()
    if modules:
        failures.append("Missing Python modules: " + ", ".join(modules))

    paths = missing_paths(root)
    if paths:
        failures.append("Missing project files: " + ", ".join(paths))

    cli_present = any(
        shutil.which(name) is not None for name in ("claude", "claude.cmd", "claude.exe")
    )
    if not cli_present:
        failures.append("Claude Code CLI was not found on PATH")
    elif not args.offline:
        # Presence is not readiness. An expired token passes every static check
        # and then kills the first reviewer, after the parse has already run.
        print("[check] probing the Claude Code CLI for a valid session ...")
        hint = probe_authentication()
        if hint:
            failures.append(hint)

    if failures:
        for failure in failures:
            print(f"[fail] {failure}", file=sys.stderr)
        print("Run the setup steps in README.md and authenticate Claude Code before reviewing papers.", file=sys.stderr)
        return 1

    print("OK: environment looks ready for the reviewer pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
