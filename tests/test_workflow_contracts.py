"""Shared workflow safety and cache contracts, including executable shell checks."""

import json
import os
import re
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

    def test_prettier_probe_failure_does_not_skip_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for manifest in (None, "{invalid json"):
                with self.subTest(manifest=manifest):
                    if manifest is not None:
                        (root / "package.json").write_text(manifest)
                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", self.steps["Prettier check"]["run"]],
                        cwd=root, env={**os.environ, "PACKAGE_MANAGER": "npm"},
                        capture_output=True, text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("skipping", result.stdout)
            (root / "package.json").write_text('{"scripts": {}}')
            node = root / "node"
            node.write_text("#!/bin/sh\nexit 42\n")
            node.chmod(0o755)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", self.steps["Prettier check"]["run"]],
                cwd=root, env={**os.environ, "PACKAGE_MANAGER": "npm",
                               "PATH": str(root) + os.pathsep + os.environ["PATH"]},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 42)
            self.assertNotIn("skipping", result.stdout)


class SharedWorkflowTests(unittest.TestCase):
    def test_github_script_data_is_passed_through_matching_environment_keys(self):
        import re

        workflow_dir = ROOT / ".github" / "workflows"
        workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
        checked_steps = 0
        for path in workflow_paths:
            config = yaml.safe_load(path.read_text()) or {}
            for job_name, job in config.get("jobs", {}).items():
                for step_index, step in enumerate(job.get("steps", [])):
                    if not step.get("uses", "").startswith("actions/github-script@"):
                        continue
                    checked_steps += 1
                    context = f"{path.name}:{job_name}:step-{step_index}"
                    script = step.get("with", {}).get("script", "")
                    env = step.get("env", {})
                    with self.subTest(step=context):
                        # GitHub expressions in script source are interpolated before
                        # JavaScript parsing. Pass dynamic values via env instead.
                        self.assertNotIn("${{", script)
                        referenced = set(
                            re.findall(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)", script)
                            + re.findall(
                                r"""process\.env\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]""",
                                script,
                            )
                        )
                        with self.subTest(step=context, env_references=sorted(referenced)):
                            self.assertTrue(referenced <= set(env))
                        expression_keys = {
                            key for key, value in env.items()
                            if isinstance(value, str) and "${{" in value
                        }
                        with self.subTest(step=context, expression_env=sorted(expression_keys)):
                            self.assertTrue(expression_keys <= referenced)
        self.assertGreater(checked_steps, 0)

    def test_github_script_runner_floor_is_documented(self):
        for workflow_name in ("reusable-issue-triage.yml", "released.yml"):
            config = workflow(workflow_name)
            on = config.get("on") or config.get(True)
            description = on["workflow_call"]["inputs"]["runner-json"]["description"]
            with self.subTest(workflow=workflow_name):
                self.assertIn("v2.327.1 or newer", description)
        self.assertIn("v2.327.1 or newer", (ROOT / "README.md").read_text())

    def test_issue_triage_label_script_treats_inputs_as_literal_data(self):
        job = workflow("reusable-issue-triage.yml")["jobs"]["triage-issue"]
        step = next(step for step in job["steps"] if step.get("name") == "Add triage label")
        script = step["with"]["script"]
        self.assertNotIn("${{", script)
        self.assertEqual(step["env"]["BYPASS_LABEL"], "${{ inputs.bypass-label }}")
        self.assertEqual(step["env"]["TRIAGE_LABEL"], "${{ inputs.triage-label }}")
        triage = "needs-triage'\n${{ inputs.triage-label }}"
        bypass = "ready'\n${{ inputs.bypass-label }}"
        for labels, expect_call in ((["bug"], True), ([bypass], False), ([bypass, "bug"], False)):
            with self.subTest(labels=labels):
                labels_js = json.dumps([{"name": name} for name in labels])
                wrapper = (
                    "const calls = [];\n"
                    "const context = {repo: {owner: 'example', repo: 'app'}, "
                    "payload: {issue: {number: 7, labels: " + labels_js + "}}};\n"
                    "const github = {rest: {issues: {addLabels: async (args) => {calls.push(args);}}}};\n"
                    "const core = {info: () => {}, warning: () => {}, "
                    "setFailed: (message) => {throw Error(message);}};\n"
                    "(async () => {\ntry {\n" + script + "\n} finally {\n"
                    "console.log(JSON.stringify(calls));\n}\n})();\n"
                )
                result = subprocess.run(
                    ["node", "-e", wrapper], check=True, capture_output=True, text=True,
                    env={**os.environ, "BYPASS_LABEL": bypass, "TRIAGE_LABEL": triage},
                )
                calls = json.loads(result.stdout)
                if expect_call:
                    self.assertEqual(calls, [{
                        "owner": "example", "repo": "app", "issue_number": 7, "labels": [triage],
                    }])
                else:
                    self.assertEqual(calls, [])

    def test_github_script_jobs_have_finite_timeouts(self):
        for workflow_name in ("reusable-issue-triage.yml", "released.yml"):
            jobs = workflow(workflow_name)["jobs"]
            self.assertTrue(jobs)
            for job_name, job in jobs.items():
                with self.subTest(workflow=workflow_name, job=job_name):
                    timeout = job.get("timeout-minutes")
                    self.assertIsInstance(timeout, int)
                    self.assertGreater(timeout, 0)

    def test_readiness_stages_companion_at_same_sha_before_running(self):
        job = workflow("reusable-agent-readiness.yml")["jobs"]["agent-readiness"]
        checkout, stage, score = job["steps"]
        self.assertEqual(checkout["with"]["fetch-depth"], 1)
        self.assertEqual(checkout["with"]["filter"], "blob:none")
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertFalse(checkout["with"]["lfs"])
        self.assertEqual(job["env"]["GIT_LFS_SKIP_SMUDGE"], "1")
        self.assertEqual(score["if"], "steps.stage.outputs.staged == 'true'")
        self.assertEqual(score["env"]["AGENT_READINESS_GIT_TOKEN"], "${{ github.token }}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curl = root / "curl"
            curl.write_text('''#!/bin/bash
printf '%s\\n' "$@" >> "$RUNNER_TEMP/fetches"
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then shift; destination="$1"; fi
  shift
done
case "$destination" in
  *agent_readiness_git.py) [ "$HELPER_AVAILABLE" = true ] || exit 22;;
esac
printf '# staged fixture\\n' > "$destination"
''')
            curl.chmod(0o755)
            for enforced, available in ((False, False), (True, False), (True, True)):
                with self.subTest(enforced=enforced, available=available):
                    (root / ".agent-readiness.json").write_text(json.dumps({"enforce": enforced}))
                    (root / "output").write_text("")
                    (root / "fetches").write_text("")
                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", stage["run"]], cwd=root,
                        env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                             "JOB_WORKFLOW_SHA": "a" * 40, "RUNNER_TEMP": directory,
                             "HELPER_AVAILABLE": str(available).lower(),
                             "GITHUB_REPOSITORY": "example/app", "GITHUB_OUTPUT": str(root / "output"),
                             "GITHUB_STEP_SUMMARY": str(root / "summary")}, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1 if enforced and not available else 0)
                    self.assertIn(f"staged={str(available).lower()}", (root / "output").read_text())
                    fetches = (root / "fetches").read_text()
                    for filename in ("agent_readiness.py", "agent_readiness_git.py"):
                        self.assertIn(f"/.github/{'a' * 40}/scripts/{filename}", fetches)

    def test_staging_uses_reusable_workflow_sha_and_rejects_missing_identity(self):
        for name in ("reusable-pr-gatekeeper.yml", "reusable-agent-readiness.yml"):
            config = workflow(name)
            steps = next(iter(config["jobs"].values()))["steps"]
            stage = next(step for step in steps if "JOB_WORKFLOW_SHA" in step.get("env", {}))
            self.assertEqual(stage["env"]["JOB_WORKFLOW_SHA"], "${{ job.workflow_sha }}")
            with tempfile.TemporaryDirectory() as directory:
                for sha in ("", "main", "a" * 39, "a" * 40, "a" * 40 + "\nstaged=true", "\n" + "a" * 40):
                    with self.subTest(workflow=name, sha=sha):
                        # Stub network fetch: a valid SHA reaches curl with that
                        # exact ref; malformed identities never make a request.
                        root = Path(directory)
                        curl = root / "curl"
                        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/fetch"\nexit 1\n')
                        curl.chmod(0o755)
                        fetch = root / "fetch"
                        fetch.unlink(missing_ok=True)
                        (root / "output").write_text("")
                        (root / "summary").write_text("")
                        result = subprocess.run(
                            ["bash", "-euo", "pipefail", "-c", stage["run"]], cwd=root,
                            env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                                 "JOB_WORKFLOW_SHA": sha, "RUNNER_TEMP": directory,
                                 "GITHUB_REPOSITORY": "example/app", "GITHUB_OUTPUT": str(root / "output"),
                                 "GITHUB_STEP_SUMMARY": str(root / "summary")},
                            capture_output=True, text=True,
                        )
                        self.assertEqual(fetch.exists(), len(sha) == 40)
                        if fetch.exists():
                            self.assertIn(f"/.github/{sha}/scripts/", fetch.read_text())
                        self.assertEqual(result.returncode, 0 if "agent-readiness" in name else 1)
                        if "agent-readiness" in name:
                            self.assertIn("Agent readiness unavailable", (root / "summary").read_text())
                            self.assertIn("reason=", (root / "output").read_text())

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
        job = config["jobs"]["build-and-push"]
        self.assertEqual(job["env"]["CACHE_SCOPE"], "${{ inputs.cache-scope || inputs.image-name }}")
        self.assertEqual(job["steps"][0]["name"], "Validate cache scope")
        build = job["steps"][-1]["with"]
        for key in ("cache-from", "cache-to"):
            self.assertIn("scope=${{ env.CACHE_SCOPE }}", build[key])
        for scope, valid in (("ghcr.io/org/my-image", True), ("image:variant_2", True),
                             ("", False), ("   ", False), ("safe,mode=max", False),
                             ("safe\nmode=max", False), ("safe=unsafe", False)):
            with self.subTest(scope=scope):
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", job["steps"][0]["run"]],
                                        env={**os.environ, "CACHE_SCOPE": scope}, capture_output=True)
                self.assertEqual(result.returncode == 0, valid)

    def test_link_checker_needs_no_write_token(self):
        self.assertEqual(workflow("reusable-link-check.yml")["permissions"], {"contents": "read"})

    def test_link_checker_passes_only_inputs_the_pinned_action_declares(self):
        # Inputs declared by lychee-action's action.yml at each pinned SHA
        # (v2.9.0). A key outside this set is silently ignored by GitHub, which
        # is how `config:` went unnoticed. Update the table with the pin.
        declared = {
            "e7477775783ea5526144ba13e8db5eec57747ce8": {
                "args", "debug", "fail", "failIfEmpty", "format", "jobSummary",
                "lycheeVersion", "output", "checkbox", "token", "workingDirectory",
            },
        }
        path = ROOT / ".github" / "workflows" / "reusable-link-check.yml"
        lines = path.read_text().splitlines()
        steps = [step for step in workflow("reusable-link-check.yml")["jobs"]["link-checker"]["steps"]
                 if step.get("uses", "").startswith("lycheeverse/lychee-action@")]
        self.assertEqual(len(steps), 1)
        sha = steps[0]["uses"].split("@")[1]
        self.assertIn(sha, declared, "unknown lychee-action pin: add its action.yml inputs here")
        self.assertEqual(set(steps[0]["with"]) - declared[sha], set())
        line = next(text for text in lines if sha in text)
        self.assertTrue(line.rstrip().endswith("# v2.9.0"), line)

    def run_config_step(self, files, config_file=".lychee.toml", args="--verbose './**/*.md'"):
        job = workflow("reusable-link-check.yml")["jobs"]["link-checker"]
        step = next(step for step in job["steps"] if step.get("id") == "config")
        self.assertEqual(step["env"], {"CONFIG_FILE": "${{ inputs.config-file }}",
                                       "LYCHEE_ARGS": "${{ inputs.args }}"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in files:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("")
            output = root / "github-output"
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", step["run"]], cwd=root, capture_output=True,
                text=True, env={**os.environ, "CONFIG_FILE": config_file, "LYCHEE_ARGS": args,
                                "GITHUB_OUTPUT": str(output)},
            )
            written = output.read_text() if output.exists() else ""
        return result, written

    @staticmethod
    def parse_output(written):
        head, _, rest = written.partition("\n")
        name, _, delimiter = head.partition("<<")
        assert name == "args" and delimiter, written
        assert rest.endswith("\n" + delimiter + "\n"), written
        return rest[:-len(delimiter) - 2]

    def test_link_checker_adds_config_only_when_the_file_exists(self):
        args = "--verbose './**/*.md'"
        cases = (
            ((".lychee.toml",), ".lychee.toml", args, f"--config .lychee.toml {args}"),
            ((), ".lychee.toml", args, args),
            ((".lychee.toml",), "", args, args),
            ((".lychee.toml",), ".lychee.toml", "--config other.toml './**/*.md'", "--config other.toml './**/*.md'"),
            (("ci/lychee.toml",), "ci/lychee.toml", args, f"--config ci/lychee.toml {args}"),
            ((".lychee.toml",), ".lychee.toml", "--config=other.toml './**/*.md'", "--config=other.toml './**/*.md'"),
            ((".lychee.toml",), ".lychee.toml", "--exclude 'x--config-y' './**/*.md'",
             "--config .lychee.toml --exclude 'x--config-y' './**/*.md'"),
        )
        for files, config_file, given, expected in cases:
            with self.subTest(files=files, config_file=config_file, args=given):
                result, written = self.run_config_step(files, config_file, given)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.parse_output(written), expected)

    def test_link_checker_args_cannot_forge_other_outputs(self):
        hostile = "--verbose\nLYCHEE_ARGS_EOF\nname=value"
        result, written = self.run_config_step((".lychee.toml",), ".lychee.toml", hostile)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.parse_output(written), f"--config .lychee.toml {hostile}")
        result, _ = self.run_config_step((), "bad\n::error::x.toml", "--verbose")
        self.assertNotIn("bad", result.stdout)

    def test_link_checker_rejects_config_paths_that_args_would_eval(self):
        for name in ("a b.toml", "x;touch pwned.toml", "$(id).toml", "-x.toml", "q'.toml"):
            with self.subTest(name=name):
                result, written = self.run_config_step((name,), name)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(written, "")

    def test_static_analysis_caller_grants_read_only_contents(self):
        self.assertEqual(workflow("call-reusable-static-analysis.yml")["permissions"],
                         {"contents": "read"})

    def test_readme_lists_every_reusable_workflow(self):
        readme = (ROOT / "README.md").read_text()
        listed = set(re.findall(r"^- `(reusable-[a-z-]+\.yml)`$", readme, re.MULTILINE))
        actual = {path.name for path in (ROOT / ".github" / "workflows").glob("reusable-*.yml")}
        self.assertEqual(listed, actual)

    def test_public_suite_discovers_tests_and_cancels_only_superseded_prs(self):
        config = workflow("public-test.yml")
        runs = [step.get("run", "") for step in config["jobs"]["test"]["steps"]]
        self.assertIn("python3 -m unittest discover -s tests -p 'test_*.py'", runs)
        self.assertIn("github.event.pull_request.number || github.ref", config["concurrency"]["group"])
        self.assertEqual(config["concurrency"]["cancel-in-progress"],
                         "${{ github.event_name == 'pull_request' }}")


if __name__ == "__main__":
    unittest.main()
