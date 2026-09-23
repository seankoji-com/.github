#!/usr/bin/env python3
"""Exercise recovery delivery when GitHub accepts a dispatch without a run."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/reusable-persona-recovery-request.yml"


class RecoveryRequestTests(unittest.TestCase):
    def test_caller_uses_only_pr_identity_and_is_advisory(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        caller = yaml.safe_load((ROOT / ".github/workflows/call-reusable-pr-gatekeeper.yml").read_text())
        self.assertRegex(caller["jobs"]["recover-persona"]["uses"],
                         r"@" + r"[0-9a-f]{40}$")
        self.assertEqual(caller["jobs"]["recover-persona"]["secrets"],
                         {"SEANKOJI_CI_PRIVATE_KEY": "${{ secrets.SEANKOJI_CI_PRIVATE_KEY }}"})
        job = workflow["jobs"]["request"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertFalse(any(s.get("uses", "").startswith("actions/checkout") for s in job["steps"]))
        self.assertTrue(all(s.get("continue-on-error") for s in job["steps"] if s.get("id")))
        expected_env = {"TARGET_REPO": "${{ inputs.target_repository }}",
                        "TARGET_PR": "${{ inputs.pr_number }}",
                        "TARGET_SHA": "${{ inputs.head_sha }}"}
        for step_id in ("validate", "dispatch"):
            step = next(s for s in job["steps"] if s.get("id") == step_id)
            self.assertEqual({key: step["env"][key] for key in expected_env}, expected_env)
        request = next(s for s in job["steps"] if s.get("id") == "dispatch")
        self.assertIn(".id > $before", request["run"])
        self.assertIn("no workflow run appeared", request["run"])

    def test_validate_rejects_bad_target_before_token_mint(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        script = next(s for s in workflow["jobs"]["request"]["steps"]
                      if s.get("id") == "validate")["run"]
        valid = {"TARGET_REPO": "seankoji-com/example", "TARGET_PR": "42",
                 "TARGET_SHA": "a" * 40}
        cases = ((valid, 0),
                 (dict(valid, TARGET_REPO="evil/example"), 1),
                 (dict(valid, TARGET_PR="42; curl attacker"), 1),
                 (dict(valid, TARGET_PR="042"), 1),
                 (dict(valid, TARGET_PR="0"), 1),
                 (dict(valid, TARGET_SHA="abc"), 1))
        for env_update, expected_failure in cases:
            with self.subTest(env_update=env_update):
                result = subprocess.run(["bash", "-c", script],
                                        env=dict(os.environ, **env_update),
                                        stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode != 0, bool(expected_failure), result.stderr)

    def test_accepted_without_run_retries_once_and_stays_visible(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        script = next(s for s in workflow["jobs"]["request"]["steps"]
                      if s.get("id") == "dispatch")["run"]
        for mode, expected_posts, expected_rc in (("immediate", 1, 0),
                                                   ("second", 2, 0),
                                                   ("missing", 2, 1)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                (temp / "posts").write_text("0")
                gh = temp / "gh"
                gh.write_text('''#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"--input -"* ]]; then
  cat >/dev/null
  n=$(cat "$TEST_STATE/posts")
  echo "$((n + 1))" > "$TEST_STATE/posts"
elif [[ "$*" == *"--jq"* ]]; then
  echo 10
else
  n=$(cat "$TEST_STATE/posts")
  if { [ "$TEST_MODE" = immediate ] && [ "$n" -ge 1 ]; } ||
     { [ "$TEST_MODE" = second ] && [ "$n" -ge 2 ]; }; then
    printf '{"workflow_runs":[{"id":11,"display_title":"Persona recovery seankoji-com/example#42 @ %s"}]}\\n' "$TARGET_SHA"
  else
    echo '{"workflow_runs":[]}'
  fi
fi
''')
                gh.chmod(0o755)
                sleep = temp / "sleep"
                sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
                sleep.chmod(0o755)
                env = dict(os.environ, PATH=f"{temp}:{os.environ['PATH']}",
                           TEST_STATE=str(temp), TEST_MODE=mode,
                           TARGET_REPO="seankoji-com/example", TARGET_PR="42",
                           TARGET_SHA="a" * 40)
                result = subprocess.run(["bash", "-c", script], env=env,
                                        stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, expected_rc, result.stderr)
                self.assertEqual(int((temp / "posts").read_text()), expected_posts)
                if mode == "missing":
                    self.assertIn("no workflow run appeared", result.stdout)


if __name__ == "__main__":
    unittest.main()
