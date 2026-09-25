#!/usr/bin/env python3
"""Exercise recovery delivery when GitHub accepts a dispatch without a run."""
import json
import os
from pathlib import Path
import shutil
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
        recovery = caller["jobs"]["recover-persona"]
        self.assertEqual(recovery["uses"],
                         "seankoji-com/.github/.github/workflows/reusable-persona-recovery-request.yml@main")
        self.assertEqual(recovery["secrets"],
                         {"SEANKOJI_CI_PRIVATE_KEY": "${{ secrets.SEANKOJI_CI_PRIVATE_KEY }}"})
        self.assertTrue(recovery.get("continue-on-error"))
        self.assertEqual(recovery["if"], "github.event_name == 'pull_request_target'")
        self.assertEqual(recovery["with"], {
            "target_repository": "${{ github.repository }}",
            "pr_number": "${{ format('{0}', github.event.pull_request.number) }}",
            "head_sha": "${{ github.event.pull_request.head.sha }}",
        })
        job = workflow["jobs"]["request"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 10)
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "read"})
        self.assertFalse(any(s.get("uses", "").startswith("actions/checkout") for s in job["steps"]))
        self.assertTrue(all(s.get("continue-on-error") for s in job["steps"] if s.get("id")))
        token_step = next(s for s in job["steps"] if s.get("id") == "token")
        self.assertEqual(token_step.get("if"), "steps.validate.outcome == 'success'")
        dispatch_step = next(s for s in job["steps"] if s.get("id") == "dispatch")
        self.assertEqual(dispatch_step.get("if"), "steps.token.outcome == 'success'")
        expected_env = {"TARGET_REPO": "${{ inputs.target_repository }}",
                        "TARGET_PR": "${{ inputs.pr_number }}",
                        "TARGET_SHA": "${{ inputs.head_sha }}"}
        for step_id in ("validate", "dispatch"):
            step = next(s for s in job["steps"] if s.get("id") == step_id)
            self.assertEqual({key: step["env"][key] for key in expected_env}, expected_env)
        self.assertEqual(dispatch_step["env"]["GITHUB_TOKEN"], "${{ github.token }}")
        self.assertIn(".id > $before", dispatch_step["run"])
        self.assertIn("no matching workflow run appeared", dispatch_step["run"])

    def test_validate_rejects_bad_target_before_token_mint(self):
        if not shutil.which("bash"):
            raise unittest.SkipTest("bash required for subprocess test")
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
                if expected_failure:
                    self.assertIn("::error::", result.stdout + result.stderr)

    def test_accepted_without_run_retries_once_and_stays_visible(self):
        if not shutil.which("bash") or not shutil.which("jq"):
            raise unittest.SkipTest("bash and jq required for subprocess test")
        workflow = yaml.safe_load(WORKFLOW.read_text())
        script = next(s for s in workflow["jobs"]["request"]["steps"]
                      if s.get("id") == "dispatch")["run"]
        for mode, expected_posts, expected_rc in (("immediate", 1, 0),
                                                   ("second", 2, 0),
                                                   ("missing", 2, 1),
                                                   ("head_moved", 0, 0),
                                                   ("in_flight", 0, 0)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                (temp / "posts").write_text("0")
                gh = temp / "gh"
                gh.write_text("""#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"/pulls/"* ]]; then
  if [ "$TEST_MODE" = "head_moved" ]; then
    echo "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  else
    echo "$TARGET_SHA"
  fi
  exit 0
fi

if [[ "$*" == *"--input -"* ]]; then
  cat > "$TEST_STATE/payload.json"
  n=$(cat "$TEST_STATE/posts")
  echo "$((n + 1))" > "$TEST_STATE/posts"
  exit 0
fi

if [ "$TEST_MODE" = "in_flight" ]; then
  printf '{"workflow_runs":[{"id":10,"display_title":"Persona recovery seankoji-com/example#42 @ %s","status":"in_progress"}]}\n' "$TARGET_SHA"
  exit 0
fi

n=$(cat "$TEST_STATE/posts")
if { [ "$TEST_MODE" = immediate ] && [ "$n" -ge 1 ]; } ||
   { [ "$TEST_MODE" = second ] && [ "$n" -ge 2 ]; }; then
  printf '{"workflow_runs":[{"id":11,"display_title":"Persona recovery seankoji-com/example#42 @ %s","status":"queued"}]}\n' "$TARGET_SHA"
else
  echo '{"workflow_runs":[]}'
fi
""")
                gh.chmod(0o755)
                sleep = temp / "sleep"
                sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
                sleep.chmod(0o755)
                env = dict(os.environ, PATH=f"{temp}:{os.environ['PATH']}",
                           TEST_STATE=str(temp), TEST_MODE=mode,
                           TARGET_REPO="seankoji-com/example", TARGET_PR="42",
                           TARGET_SHA="a" * 40,
                           GH_TOKEN="app-token", GITHUB_TOKEN="job-token")
                result = subprocess.run(["bash", "-c", script], env=env,
                                        stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, expected_rc, result.stderr)
                self.assertEqual(int((temp / "posts").read_text()), expected_posts)
                if mode in ("immediate", "second"):
                    payload_path = temp / "payload.json"
                    self.assertTrue(payload_path.exists())
                    payload = json.loads(payload_path.read_text())
                    self.assertEqual(payload["event_type"], "persona-review-reconcile-requested")
                    self.assertEqual(payload["client_payload"], {
                        "repository": "seankoji-com/example",
                        "pull_number": 42,
                        "head_sha": "a" * 40,
                    })
                elif mode == "missing":
                    self.assertIn("no matching workflow run appeared", result.stdout + result.stderr)
                    self.assertIn("Recent workflow runs in .github-private:", result.stdout + result.stderr)
                elif mode == "head_moved":
                    self.assertIn("PR head has moved from", result.stdout + result.stderr)
                elif mode == "in_flight":
                    self.assertIn("Active recovery run for this exact head is already in flight", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
