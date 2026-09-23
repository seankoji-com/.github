"""Shared workflow safety and cache contracts, including executable shell checks."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


class NodeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.job = workflow("reusable-node-ci.yml")["jobs"]["validate"]
        self.steps = {step["name"]: step for step in self.job["steps"]}

    def test_package_manager_is_validated_before_checkout(self):
        self.assertEqual(self.job["steps"][0]["name"], "Validate package manager")
        self.assertEqual(self.job["env"]["PACKAGE_MANAGER"], "${{ inputs.package-manager }}")
        script = self.steps["Validate package manager"]["run"]
        for manager in ("pnpm", "npm", "yarn", "bun", "", "npm; exit 0", "$(exit 0)"):
            with self.subTest(manager=manager):
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", script], capture_output=True,
                    env={**os.environ, "PACKAGE_MANAGER": manager},
                )
                self.assertEqual(result.returncode == 0, manager in ("pnpm", "npm", "yarn"))

    def test_run_steps_do_not_interpolate_package_manager_as_shell_source(self):
        for step in self.steps.values():
            self.assertNotIn("${{ inputs.package-manager }}", step.get("run", ""))

    def test_prettier_uses_exact_script_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = root / "manager"
            manager.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > invoked\n')
            manager.chmod(0o755)
            for scripts, should_run in (({}, False), ({"prettier:check:other": "true"}, False),
                                        ({"prettier:check": "true"}, True)):
                with self.subTest(scripts=scripts):
                    (root / "package.json").write_text(json.dumps({"scripts": scripts}))
                    subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", self.steps["Prettier check"]["run"]],
                        cwd=root, env={**os.environ, "PACKAGE_MANAGER": str(manager)},
                        check=True, capture_output=True,
                    )
                    self.assertEqual((root / "invoked").exists(), should_run)
                    if should_run:
                        self.assertEqual((root / "invoked").read_text(), "run prettier:check\n")


class SharedWorkflowTests(unittest.TestCase):
    def test_staging_uses_reusable_workflow_sha_and_rejects_missing_identity(self):
        for name in ("reusable-pr-gatekeeper.yml", "reusable-agent-readiness.yml"):
            config = workflow(name)
            steps = next(iter(config["jobs"].values()))["steps"]
            stage = next(step for step in steps if "JOB_WORKFLOW_SHA" in step.get("env", {}))
            self.assertEqual(stage["env"]["JOB_WORKFLOW_SHA"], "${{ job.workflow_sha }}")
            with tempfile.TemporaryDirectory() as directory:
                for sha in ("", "main", "a" * 39, "a" * 40):
                    with self.subTest(workflow=name, sha=sha):
                        # Stub network fetch: a valid SHA reaches curl with that
                        # exact ref; malformed identities never make a request.
                        root = Path(directory)
                        curl = root / "curl"
                        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/fetch"\nexit 1\n')
                        curl.chmod(0o755)
                        fetch = root / "fetch"
                        fetch.unlink(missing_ok=True)
                        result = subprocess.run(
                            ["bash", "-euo", "pipefail", "-c", stage["run"]], cwd=root,
                            env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                                 "JOB_WORKFLOW_SHA": sha, "RUNNER_TEMP": directory,
                                 "GITHUB_REPOSITORY": "example/app", "GITHUB_OUTPUT": str(root / "output")},
                            capture_output=True, text=True,
                        )
                        self.assertEqual(fetch.exists(), len(sha) == 40)
                        if fetch.exists():
                            self.assertIn(f"/.github/{sha}/scripts/", fetch.read_text())
                        self.assertEqual(result.returncode, 0 if "agent-readiness" in name else 1)

    def test_shellspec_is_pinned_and_runs_without_write_credentials(self):
        config = workflow("reusable-shellspec.yml")
        self.assertEqual(config["permissions"], {"contents": "read"})
        job = config["jobs"]["test"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 10)
        checkout, install, fetch, run = job["steps"]
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertRegex(fetch["env"]["SHELLSPEC_SHA"], r"^[a-f0-9]{40}$")
        self.assertNotIn("| sh", fetch["run"])
        self.assertIn("$RUNNER_TEMP/shellspec/shellspec", run["run"])

    def test_docker_cache_is_scoped_to_image_with_caller_override(self):
        config = workflow("reusable-docker-build-push.yml")
        inputs = config[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["cache-scope"]["default"], "")
        build = config["jobs"]["build-and-push"]["steps"][-1]["with"]
        for key in ("cache-from", "cache-to"):
            self.assertIn("scope=${{ inputs.cache-scope || inputs.image-name }}", build[key])

    def test_link_checker_needs_no_write_token(self):
        self.assertEqual(workflow("reusable-link-check.yml")["permissions"], {"contents": "read"})

    def test_public_suite_discovers_tests_and_cancels_only_superseded_prs(self):
        config = workflow("public-test.yml")
        runs = [step.get("run", "") for step in config["jobs"]["test"]["steps"]]
        self.assertIn("python3 -m unittest discover -s tests -p 'test_*.py'", runs)
        self.assertIn("github.event.pull_request.number || github.ref", config["concurrency"]["group"])
        self.assertEqual(config["concurrency"]["cancel-in-progress"],
                         "${{ github.event_name == 'pull_request' }}")


if __name__ == "__main__":
    unittest.main()
