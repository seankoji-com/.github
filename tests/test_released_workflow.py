#!/usr/bin/env python3
"""Structure contract for the shared released.yml workflow.

The workflow emits GitHub's standard deployment_status signal. This suite pins
the shape of the reusable workflow so a caller's `uses:` and `with:` block and
the deployment-status mechanism do not drift.

Hermetic: no token, no network, stdlib and PyYAML only.
"""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "released.yml"


class ReleasedWorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text()
        cls.workflow = yaml.safe_load(cls.text)

    def test_is_callable_with_expected_inputs(self):
        # YAML 1.1 parses the bare `on:` key as the boolean True.
        on = self.workflow.get("on") or self.workflow.get(True)
        self.assertIn("workflow_call", on)
        inputs = on["workflow_call"]["inputs"]

        self.assertIn("sha", inputs)
        self.assertEqual(inputs["sha"]["type"], "string")

        self.assertIn("environment", inputs)
        self.assertEqual(inputs["environment"]["type"], "string")
        self.assertEqual(inputs["environment"]["default"], "prod")

    def test_declares_deployments_write(self):
        self.assertEqual(self.workflow["permissions"].get("deployments"), "write")

    def test_emits_deployment_status_success_not_repository_dispatch(self):
        # The header comment must justify the event mechanism.
        self.assertIn("deployment_status", self.text)

        # The job must actually create a deployment + a success status for
        # it, and must not instead fire a repository_dispatch event — the
        # mechanism is a job-body property, not just prose, so check the
        # parsed script rather than the whole file (which also legitimately
        # mentions "repository_dispatch" by name in the header's rationale).
        job = next(iter(self.workflow["jobs"].values()))
        script = "\n".join(
            step["with"]["script"]
            for step in job["steps"]
            if "script" in step.get("with", {})
        )
        self.assertIn("createDeployment", script)
        self.assertIn("createDeploymentStatus", script)
        self.assertIn("state: 'success'", script)
        self.assertNotIn("createDispatchEvent", script)
        self.assertNotIn("repository_dispatch", script)

    def test_single_job_no_checkout_needed(self):
        # This workflow only calls the GitHub API for the caller's own repo;
        # it never needs the caller's source, so it should not check it out.
        jobs = self.workflow["jobs"]
        self.assertEqual(len(jobs), 1)
        for job in jobs.values():
            for step in job.get("steps", []):
                self.assertNotIn("actions/checkout", step.get("uses", ""))

    def test_documented_in_readme_with_exact_call_syntax(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("released.yml", readme)
        self.assertIn(
            "uses: seankoji-com/.github/.github/workflows/released.yml@main",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
