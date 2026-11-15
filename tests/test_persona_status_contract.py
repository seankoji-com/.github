"""Composite token transport and hosted coverage for the standalone publisher."""

import os
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class PersonaCompositeTests(unittest.TestCase):
    def test_composite_has_no_downloaded_actions_checkout_or_job(self):
        action = yaml.safe_load((ROOT / "actions/persona-status/action.yml").read_text())
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertNotIn("jobs", action)
        steps = action["runs"]["steps"]
        self.assertEqual(len(steps), 2)
        for step in steps:
            self.assertNotIn("uses", step)
            self.assertIn("python3 -I", step["run"])
            self.assertNotIn("${{", step["run"])
        self.assertEqual(steps[1]["run"], 'python3 -I "$PERSONA_SCRIPT"')
        self.assertEqual(steps[1]["env"]["PERSONA_TOKEN"], "${{ inputs.token }}")
        self.assertEqual(steps[1]["env"]["PERSONA_SCRIPT"], "${{ github.action_path }}/publish.py")
        self.assertTrue(action["inputs"]["publisher_bot_id"]["required"])
        self.assertTrue(action["inputs"]["token"]["required"])

    def test_mask_precedes_publisher_and_escapes_commands(self):
        steps = yaml.safe_load((ROOT / "actions/persona-status/action.yml").read_text())["runs"]["steps"]
        self.assertIn("Mask", steps[0]["name"])
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", steps[0]["run"]],
                                env={**os.environ, "PERSONA_TOKEN": "fixture%token\n::error::forged\r"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "::add-mask::fixture%25token%0A::error::forged%0D\n")
        self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_hosted_python310_collects_and_executes_actual_publisher_tests(self):
        job = yaml.safe_load((ROOT / ".github/workflows/public-test.yml").read_text())["jobs"]["test"]
        steps = {s["name"]: s for s in job["steps"]}
        self.assertIn("actions/persona-status/*.py", steps["Compile runner scripts under Python 3.10"]["run"])
        self.assertIn('Path("actions/persona-status")', steps["Load runner scripts under Python 3.10"]["run"])
        run = steps["Execute persona publisher tests under Python 3.10"]["run"]
        self.assertIn("test_persona_status.py", run)
        names = [s["name"] for s in job["steps"]]
        self.assertGreater(names.index("Execute persona publisher tests under Python 3.10"),
                           names.index("Set up Python 3.10 (oldest self-hosted runner)"))


if __name__ == "__main__":
    unittest.main()
