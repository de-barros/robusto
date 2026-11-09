"""Two model tiers: mechanical work on a light model, judgement on the standard one.

Also pins that config/defaults.toml is actually applied. Before the tiers, the
configured model was recorded in the run manifest but never passed to an agent,
so every agent ran on whatever the CLI defaulted to.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_paper  # noqa: E402
from claude_backend import claude_exec_command  # noqa: E402
from reviewer_config import load_reviewers_config, write_reviewers_config  # noqa: E402

LIGHT_REVIEWERS = {
    "parser_quality_auditor",
    "crossref_auditor",
    "reference_auditor",
    "grammar_auditor",
}


class ShippedDefaults(unittest.TestCase):
    def test_defaults_name_both_tiers(self) -> None:
        models = review_paper.resolve_models(review_paper.project_defaults(REPO_ROOT))
        self.assertEqual(models, {"standard": "claude-opus-5-5", "light": "claude-sonnet-5-5"})

    def test_only_the_mechanical_reviewers_are_light(self) -> None:
        reviewers = load_reviewers_config(REPO_ROOT / "config" / "reviewers.json", enabled_only=False)
        light = {r.name for r in reviewers if r.model_tier == "light"}
        self.assertEqual(light, LIGHT_REVIEWERS)
        self.assertEqual(
            next(r for r in reviewers if r.name == "devils_advocate_auditor").model_tier, "standard"
        )


class Resolution(unittest.TestCase):
    def test_command_line_overrides_each_tier(self) -> None:
        defaults = {"model": "big", "light_model": "small"}
        self.assertEqual(
            review_paper.resolve_models(defaults, "x"), {"standard": "x", "light": "small"}
        )
        self.assertEqual(
            review_paper.resolve_models(defaults, "x", "x"), {"standard": "x", "light": "x"}
        )

    def test_light_falls_back_to_standard_then_to_the_cli_default(self) -> None:
        self.assertEqual(review_paper.resolve_models({"model": "big"}), {"standard": "big", "light": "big"})
        self.assertEqual(review_paper.resolve_models({}), {"standard": None, "light": None})


class Routing(unittest.TestCase):
    def test_each_reviewer_runs_on_its_tier(self) -> None:
        reviewers = load_reviewers_config(REPO_ROOT / "config" / "reviewers.json")
        models = {"standard": "big", "light": "small"}
        seen: dict[str, str | None] = {}

        def fake_start(reviewer, repo, prompts, reviews, schema, logs, model, effort):
            seen[reviewer.name] = model
            return (reviewer,)

        def fake_wait(reviewer, timeout_seconds=None):
            return review_paper.RunResult(reviewer.name, 0, Path("o"), Path("e"))

        with mock.patch.object(review_paper, "start_reviewer", fake_start), mock.patch.object(
            review_paper, "wait_reviewer", fake_wait
        ):
            review_paper.run_reviewer_batch(
                reviewers, REPO_ROOT, Path("p"), Path("r"),
                REPO_ROOT / "schemas" / "reviewer_output.schema.json", Path("l"), models,
            )
        for name, model in seen.items():
            self.assertEqual(model, "small" if name in LIGHT_REVIEWERS else "big", name)

    def test_a_plain_model_string_still_applies_to_everyone(self) -> None:
        reviewer = load_reviewers_config(REPO_ROOT / "config" / "reviewers.json")[0]
        self.assertEqual(review_paper.model_for(reviewer, "one"), "one")

    def test_the_real_cli_command_carries_the_model(self) -> None:
        with mock.patch.dict(os.environ, {"ROBUSTO_BACKEND": "claude"}), mock.patch(
            "claude_backend.claude_command", return_value="claude"
        ):
            command = claude_exec_command(model="claude-sonnet-5-5")
        self.assertEqual(command[command.index("--model") + 1], "claude-sonnet-5-5")


class ConfigContract(unittest.TestCase):
    def write(self, tmp: str, **extra: object) -> Path:
        path = Path(tmp) / "reviewers.json"
        entry = {
            "name": "grammar_auditor",
            "prompt": "grammar_audit.txt",
            "output": "grammar_auditor.json",
            "id_prefix": "GRAM",
            "normalization_role": "copyedit",
            **extra,
        }
        path.write_text(json.dumps({"reviewers": [entry]}), encoding="utf-8")
        return path

    def test_tier_defaults_to_standard_and_survives_a_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_reviewers_config(self.write(tmp))[0].model_tier, "standard")
            loaded = load_reviewers_config(self.write(tmp, model_tier="light"))
            copy = Path(tmp) / "copy.json"
            write_reviewers_config(copy, loaded)
            self.assertEqual(load_reviewers_config(copy)[0].model_tier, "light")

    def test_unknown_tier_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "model_tier"):
                load_reviewers_config(self.write(tmp, model_tier="huge"))


if __name__ == "__main__":
    unittest.main()
