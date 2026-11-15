"""Execute the native result workflow's actual step, including fail-closed cases."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/reusable-ci-result.yml"


class NativeCIResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.job = cls.workflow["jobs"]["all-checks-passed"]
        cls.step = cls.job["steps"][0]

    def execute(self, needs, expected='["lint", "test"]', *, cwd=ROOT, extra_env=None):
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", self.step["run"]],
            cwd=cwd, env={**os.environ, "NEEDS_JSON": needs, "EXPECTED_JOBS": expected,
                          **(extra_env or {})}, capture_output=True, text=True, timeout=5,
        )

    def results(self, **overrides):
        return json.dumps({job: {"result": overrides.get(job, "success")}
                           for job in ("lint", "test")})

    def test_all_declared_jobs_must_succeed_without_logging_outputs(self):
        secret = "fixture-output-that-must-not-be-logged"
        needs = json.dumps({job: {"result": "success", "outputs": {"value": secret}}
                            for job in ("lint", "test")})
        result = self.execute(needs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("All 2 native CI dependencies succeeded", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_every_dependency_can_block_the_result(self):
        for job in ("lint", "test"):
            for status in ("failure", "cancelled", "skipped", "pending", "in_progress", "neutral", "", None):
                with self.subTest(job=job, status=status):
                    result = self.execute(self.results(**{job: status}))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("CI dependency " + job + " did not succeed", result.stdout)
                    if status == "cancelled":
                        self.assertIn("possibly a superseded run", result.stdout)

    def test_missing_extra_or_empty_native_dependency_inventory_fails(self):
        for needs in ({}, {"lint": {"result": "success"}},
                      {job: {"result": "success"} for job in ("lint", "test", "extra")}):
            with self.subTest(needs=needs):
                result = self.execute(json.dumps(needs))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("do not match expected_jobs", result.stdout)

    def test_expected_jobs_requires_distinct_lowercase_ids(self):
        for expected in ([], {}, None, "lint", ["lint", "lint"], ["Lint", "test"],
                         ["lint", 1], ["lint", "bad\n::warning::value"], ["lint", ""]):
            with self.subTest(expected=expected):
                result = self.execute(self.results(), json.dumps(expected))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("expected_jobs must be", result.stdout)

    def test_invalid_json_and_duplicate_keys_fail_without_echoing_input(self):
        for needs, expected in (("not-json", '["lint"]'), ("{}", "not-json"),
                                ('{"lint":{"result":"failure"},"lint":{"result":"success"}}', '["lint"]'),
                                ('{"lint":{"result":"failure","result":"success"}}', '["lint"]')):
            with self.subTest(needs=needs, expected=expected):
                result = self.execute(needs, expected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid native CI result JSON", result.stdout)

    def test_malformed_records_and_unknown_results_fail(self):
        for record in (None, [], "success", True, {}, {"result": ["success"]},
                       {"result": {"state": "success"}}):
            with self.subTest(record=record):
                result = self.execute(json.dumps({"lint": record}), '["lint"]')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid result", result.stdout)

    def test_hostile_payload_cannot_execute_or_forge_log_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "executed"
            attack = "$(touch " + str(marker) + ")\n::warning::forged"
            for needs, expected in ((json.dumps({attack: {"result": "success"}}), json.dumps([attack])),
                                    (json.dumps({"lint": {"result": attack}}), '["lint"]')):
                result = self.execute(needs, expected)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(attack, result.stdout + result.stderr)
                self.assertNotIn("::warning::forged", result.stdout + result.stderr)
                self.assertFalse(marker.exists())

    def test_isolated_python_ignores_workspace_and_python_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "json.py").write_text('raise RuntimeError("workspace import executed")\n')
            result = self.execute(self.results(), cwd=directory,
                                  extra_env={"PYTHONPATH": directory, "PYTHONHOME": directory})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("workspace import executed", result.stdout + result.stderr)

    def test_workflow_is_one_unconditional_job_with_no_token_or_checkout(self):
        self.assertEqual(list(self.workflow["jobs"]), ["all-checks-passed"])
        self.assertEqual(self.job["name"], "all-checks-passed")
        self.assertNotIn("if", self.job)
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["timeout-minutes"], 1)
        self.assertEqual(len(self.job["steps"]), 1)
        self.assertNotIn("uses", self.step)
        self.assertEqual(self.step["env"], {"NEEDS_JSON": "${{ inputs.needs_json }}",
                                            "EXPECTED_JOBS": "${{ inputs.expected_jobs }}"})
        self.assertIn("python3 -I - <<'PY'", self.step["run"])
        self.assertNotIn("${{", self.step["run"])

    def test_hosted_smoke_has_exact_dependencies_and_empty_permissions(self):
        jobs = yaml.safe_load((ROOT / ".github/workflows/public-test.yml").read_text())["jobs"]
        smoke = jobs["verify-ci-result"]
        self.assertEqual(smoke["needs"], ["test"])
        self.assertEqual(smoke["if"], "always()")
        self.assertNotIn("name", smoke)
        self.assertEqual(smoke["permissions"], {})
        self.assertEqual(smoke["uses"], "./.github/workflows/reusable-ci-result.yml")
        self.assertEqual(smoke["with"]["needs_json"], "${{ toJSON(needs) }}")
        self.assertEqual(json.loads(smoke["with"]["expected_jobs"]), smoke["needs"])
        self.assertEqual(json.loads(smoke["with"]["runner-json"]), ["ubuntu-latest"])


if __name__ == "__main__":
    unittest.main()
