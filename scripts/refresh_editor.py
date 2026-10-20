from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline_paths import paper_run_paths
from review_paper import enforce_preflight_gate, run_editor_pass
from reviewer_config import load_reviewers_config, write_reviewers_config


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def require_paths(paths: dict[str, Path]) -> None:
    missing = [f"{name}: {path}" for name, path in paths.items() if not path.exists()]
    if missing:
        raise FileNotFoundError("Editor refresh prerequisites are missing:\n- " + "\n- ".join(missing))


def run_command(command: list[str], cwd: Path, *, input_text: str | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        input=input_text,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def validated_editor_roster(
    selected: list, reviews_dir: Path, repo: Path, *, strict: bool
) -> tuple[list, dict[str, str]]:
    """Validate each selected reviewer's output and return the editor's roster.

    A preflight reviewer must validate. A substantive reviewer whose output is
    missing or invalid is dropped, and the editor works from the rest, unless
    ``strict`` is set, in which case the first such reviewer stops the refresh.
    """
    roster = []
    dropped: dict[str, str] = {}
    for reviewer in selected:
        output = reviews_dir / reviewer.output
        reason = None
        if not output.exists():
            reason = f"missing output {output.relative_to(repo)}"
        else:
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/validate_review_json.py",
                    "--schema",
                    "schemas/reviewer_output.schema.json",
                    "--input",
                    str(output.relative_to(repo)),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0:
                lines = [line[2:] for line in completed.stdout.splitlines() if line.startswith("- ")]
                reason = "invalid output: " + ("; ".join(lines[:3]) or completed.stdout.strip()[:200])
        if reason is not None and (strict or reviewer.stage == "preflight"):
            raise RuntimeError(f"{reviewer.name}: {reason}")
        if reason is not None:
            dropped[reviewer.name] = reason
            continue
        if reviewer.stage == "preflight":
            enforce_preflight_gate(reviewer, output)
        roster.append(reviewer)
    if not any(reviewer.stage == "review" for reviewer in roster):
        raise RuntimeError("No substantive reviewer output validated; nothing for the editor to synthesise.")
    return roster, dropped


def mark_run_manifest_complete(
    manifest_path: Path,
    report: Path,
    repo: Path,
    reviewer_names: list[str],
    dropped: dict[str, str] | None = None,
) -> None:
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    now = datetime.now(timezone.utc).isoformat()
    manifest.update(
        {
            "status": "complete",
            "completed_at_utc": now,
            "synthesis_refreshed_at_utc": now,
            "selected_reviewers": reviewer_names,
            "report": str(report.relative_to(repo)),
        }
    )
    if dropped:
        manifest["dropped_reviewers"] = [
            {"name": name, "reason": reason} for name, reason in dropped.items()
        ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate existing reviews, rebuild the normalized bundle, and refresh editor output."
    )
    parser.add_argument("--paper-id", required=True)
    parser.add_argument(
        "--run-editor",
        action="store_true",
        help="Run the editor pass and final report check. By default only rerenders prompts and rebuilds editor input.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Advanced testing override for the editor model when --run-editor is used. "
            "The default comes from config/defaults.toml."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high", "xhigh", "max"],
        default=None,
        help=(
            "Accepted for compatibility; Claude Code has no reasoning-effort control, so it is ignored."
        ),
    )
    parser.add_argument(
        "--strict-reviewers",
        action="store_true",
        help=(
            "Stop when any reviewer output is missing or invalid. By default a substantive "
            "reviewer that fails validation is dropped and the editor works from the rest."
        ),
    )
    parser.add_argument(
        "--agent-timeout-minutes",
        type=float,
        default=45.0,
        help="Timeout for the editor agent and its one repair pass. Default: 45 minutes.",
    )
    parser.add_argument(
        "--max-editor-input-bytes",
        type=int,
        default=None,
        help="Override the editor input budget passed to build_editor_input.py --max-bytes.",
    )
    args = parser.parse_args()

    repo = repo_root()
    paths = paper_run_paths(repo, args.paper_id)
    parsed_dir = paths.parsed_dir
    reviews_dir = paths.reviews_dir
    prompts_dir = paths.prompts_dir
    editor_dir = paths.editor_dir
    outputs_dir = paths.outputs_dir
    selected_reviewers = paths.selected_reviewers_config_path
    bundle = paths.bundle_path
    editor_prompt = paths.editor_prompt_path
    editor_input = paths.editor_input_path
    report = paths.report_path

    require_paths(
        {
            "parsed artifacts": parsed_dir,
            "reviews directory": reviews_dir,
            "selected reviewers": selected_reviewers,
        }
    )
    prompts_dir.mkdir(parents=True, exist_ok=True)
    editor_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    selected = load_reviewers_config(selected_reviewers)
    editor_roster, dropped = validated_editor_roster(
        selected, reviews_dir, repo, strict=args.strict_reviewers
    )
    for name, why in dropped.items():
        print(f"[drop] {name}: {why}")
    if dropped:
        roster_path = editor_dir / "editor_reviewers.json"
        write_reviewers_config(roster_path, editor_roster)
    else:
        roster_path = selected_reviewers
    roster_relative = str(roster_path.relative_to(repo))
    review_stage = [reviewer for reviewer in editor_roster if reviewer.stage == "review"]

    run_command(
        [
            sys.executable,
            "scripts/normalize_review_outputs.py",
            "--paper-id",
            args.paper_id,
            "--reviews-dir",
            str(reviews_dir.relative_to(repo)),
            "--output",
            str(bundle.relative_to(repo)),
            "--reviewers-config",
            roster_relative,
        ],
        repo,
    )

    run_command(
        [
            sys.executable,
            "scripts/render_prompts.py",
            "--paper-id",
            args.paper_id,
            "--parsed-dir",
            str(parsed_dir.relative_to(repo)),
            "--reviews-dir",
            str(reviews_dir.relative_to(repo)),
            "--schema-path",
            "schemas/reviewer_output.schema.json",
            "--output-dir",
            str(prompts_dir.relative_to(repo)),
            "--editor-bundle-path",
            str(bundle.relative_to(repo)),
            "--reviewers-config",
            roster_relative,
        ],
        repo,
    )
    build_command = [
        sys.executable,
        "scripts/build_editor_input.py",
        "--paper-id",
        args.paper_id,
        "--editor-prompt",
        str(editor_prompt.relative_to(repo)),
        "--bundle",
        str(bundle.relative_to(repo)),
        "--reviews-dir",
        str(reviews_dir.relative_to(repo)),
        "--output",
        str(editor_input.relative_to(repo)),
        "--reviewers-config",
        roster_relative,
    ]
    if args.max_editor_input_bytes is not None:
        build_command += ["--max-bytes", str(args.max_editor_input_bytes)]
    run_command(build_command, repo)

    if args.run_editor:
        # The editor's report is its stdout, exactly as in review_paper.py. The
        # Codex-era --output-last-message flag this used to pass does not exist
        # in the Claude Code CLI, so every --run-editor call failed on it.
        run_editor_pass(
            repo=repo,
            log_dir=paths.log_dir,
            editor_input_path=editor_input,
            report_path=report,
            bundle_path=bundle,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.agent_timeout_minutes * 60,
        )
        mark_run_manifest_complete(
            paths.run_manifest_path,
            report,
            repo,
            [reviewer.name for reviewer in review_stage],
            dropped,
        )

    print(f"editor input refreshed: {editor_input.relative_to(repo)}")
    if args.run_editor:
        print(f"report refreshed: {report.relative_to(repo)}")
    else:
        print("editor was not run; pass --run-editor to refresh the final report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
