"""Exercise the actual reusable-workflow installers without network or host pip."""

import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/reusable-static-analysis.yml"


class ScannerInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]

    def install(self, tool, *, pip_failure=False, scanner_failure=False):
        with tempfile.TemporaryDirectory(prefix="scanner-install-") as directory:
            root = Path(directory)
            binary = root / f"{tool}-venv/bin"
            binary.mkdir(parents=True)
            log = root / "calls"
            github_path = root / "github-path"
            scanner_log = root / "scanner-calls"
            python = binary / "python"
            python.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n'
                '[ "$PIP_FAIL" = 0 ] || exit 17\n'
            )
            scanner = binary / tool
            scanner.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SCANNER_LOG"\n[ "$SCANNER_FAIL" = 0 ] || exit 19\n')
            for file in (python, scanner):
                file.chmod(0o755)
            step = next(s for s in self.jobs[tool]["steps"] if s.get("id") == "install")
            result = subprocess.run(
                [shutil.which("bash"), "-c", step["run"]],
                env={"PATH": str(binary), "RUNNER_TEMP": directory,
                     "SCANNER_LOG": str(scanner_log),
                     "GITHUB_PATH": str(github_path), "CALL_LOG": str(log),
                     "PIP_FAIL": str(int(pip_failure)),
                     "SCANNER_FAIL": str(int(scanner_failure))},
                capture_output=True, text=True, timeout=5,
            )
            if not pip_failure:
                self.assertEqual(scanner_log.read_text().splitlines(), ["--version"])
            return result, log.read_text().splitlines(), (
                github_path.read_text() if github_path.exists() else ""
            )

    def test_success_uses_only_isolated_pip_and_publishes_verified_scanner(self):
        for tool, version in (("semgrep", "1.176.0"), ("zizmor", "1.30.0")):
            with self.subTest(tool=tool):
                result, calls, path = self.install(tool)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 3)
                self.assertIn("pip==25.2", calls[0])
                self.assertIn(f"{tool}=={version}", calls[1])
                self.assertEqual(calls[2], "-m pip check")
                for call in calls[:2]:
                    self.assertIn("--timeout 30 --retries 2", call)
                    self.assertIn("--only-binary=:all:", call)
                    self.assertNotIn("--break-system-packages", call)
                    self.assertNotIn("--quiet", call)
                self.assertTrue(path.strip().endswith(f"/{tool}-venv/bin"))

    def test_pip_failure_does_not_fall_back_or_publish_an_ambient_scanner(self):
        for tool in self.jobs:
            with self.subTest(tool=tool):
                result, calls, path = self.install(tool, pip_failure=True)
                self.assertEqual(result.returncode, 17)
                self.assertEqual(len(calls), 1)
                self.assertEqual(path, "")

    def test_broken_installed_executable_is_not_published(self):
        for tool in self.jobs:
            with self.subTest(tool=tool):
                result, _, path = self.install(tool, scanner_failure=True)
                self.assertEqual(result.returncode, 19)
                self.assertEqual(path, "")

    def test_failed_install_cannot_run_scan_and_each_network_phase_is_bounded(self):
        for job in self.jobs.values():
            steps = job["steps"]
            scan = next(s for s in steps if s.get("id") == "scan")
            self.assertEqual(scan["if"], "steps.install.outcome == 'success'")
            # Existing scanner advisory policy is preserved; required gates are separate.
            self.assertTrue(scan["continue-on-error"])
            for step in steps:
                if step.get("id") in {"python", "install", "scan", "cache"} or "actions/cache/save@" in step.get("uses", ""):
                    self.assertGreater(step["timeout-minutes"], 0)
                    self.assertLess(step["timeout-minutes"], job["timeout-minutes"])

    def test_install_scan_leave_overhead_and_cache_excludes_environments(self):
        for tool, job in self.jobs.items():
            caps = sum(s["timeout-minutes"] for s in job["steps"]
                       if s.get("id") in {"install", "scan"})
            self.assertLessEqual(caps + 3, job["timeout-minutes"])
            cache = next(s for s in job["steps"] if s.get("id") == "cache")
            install = next(s for s in job["steps"] if s.get("id") == "install")
            save = next(s for s in job["steps"] if "actions/cache/save@" in s.get("uses", ""))
            self.assertEqual(install["env"]["PIP_CACHE_DIR"], cache["with"]["path"])
            self.assertEqual(save["with"]["path"], cache["with"]["path"])
            self.assertEqual(save["with"]["key"], "${{ steps.cache.outputs.cache-primary-key }}")
            self.assertEqual(save["if"], "steps.install.outcome == 'success' && steps.cache.outputs.cache-hit != 'true' && steps.cache.outputs.cache-primary-key != ''")
            self.assertEqual(cache["with"]["path"], "${{ runner.temp }}/scanner-pip-cache")
            for dimension in ("runner.os", "runner.arch", "steps.python.outputs.version", tool):
                self.assertIn(dimension, cache["with"]["key"])


    def test_prepare_publishes_fresh_results_only_after_working_venv(self):
        for tool, job in self.jobs.items():
            prep = next(s for s in job["steps"] if s.get("id") == "python")
            for fail in (False, True):
                with self.subTest(tool=tool, venv_failure=fail):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        binary = root / "bin"
                        binary.mkdir()
                        python = binary / "python3"
                        python.write_text('#!/bin/sh\nif [ "$1" = -m ]; then [ "$VENV_FAIL" = 0 ] || exit 17; elif [ "$1" = -c ]; then case "$2" in *version=*) echo version=3.12;; esac; fi\n')
                        python.chmod(0o755)
                        (binary / "mktemp").symlink_to(shutil.which("mktemp"))
                        output = root / "output"
                        result = subprocess.run([shutil.which("bash"), "-c", prep["run"]],
                            env={"PATH": str(binary), "RUNNER_TEMP": directory,
                                 "GITHUB_OUTPUT": str(output), "VENV_FAIL": str(int(fail))},
                            capture_output=True, text=True, timeout=5)
                        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
                        self.assertEqual(outputs["version"], "3.12")
                        if fail:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn("::warning::", result.stdout)
                            self.assertNotIn("result", outputs)
                        else:
                            self.assertEqual(result.returncode, 0, result.stderr)
                            path = Path(outputs["result"])
                            self.assertEqual(path.parent.parent, root)
                            self.assertTrue(path.parent.is_dir())
                            self.assertFalse(path.exists())

    @unittest.skipUnless(shutil.which("jq"), "jq is required to execute scanner summaries")
    def test_summary_rejects_stale_workspace_and_malformed_output(self):
        import json
        for tool, job in self.jobs.items():
            summary_step = next(s for s in job["steps"] if s["name"] == "Record scan outcome")
            prep = next(s for s in job["steps"] if s.get("id") == "python")
            self.assertIn('mktemp -d "$RUNNER_TEMP/', prep["run"])
            artifact = next(s for s in job["steps"] if s["name"] == "Upload results artifact")
            self.assertEqual(artifact["with"]["path"], "${{ steps.python.outputs.result }}")
            self.assertIn("steps.install.outcome == 'success'", artifact["if"])
            for install, scan, output, expected in (
                ("failure", "success", [{"finding": True}], "NO-OUTPUT"),
                ("", "", [], "NO-OUTPUT"),
                ("success", "", [], "NO-OUTPUT"),
                ("success", "skipped", [], "NO-OUTPUT"),
                ("success", "failure", "malformed", "NO-OUTPUT"),
                ("success", "success", {"bad": 1}, "NO-OUTPUT"),
                ("success", "success", [], "SCANNED: 0"),
                ("success", "failure", [{"finding": True}], "SCANNED: 1"),
            ):
                with self.subTest(tool=tool, install=install, scan=scan, output=output):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        (root / f"{tool}-results.json").write_text('{"results": []}' if tool == "semgrep" else '[]')
                        fresh = root / "unique-temp" / "results.json"
                        fresh.parent.mkdir()
                        if output is not None:
                            if output == "malformed":
                                fresh.write_text("bad json")
                            else:
                                fresh.write_text(json.dumps({"results": output} if tool == "semgrep" else output))
                        summary = root / "summary"
                        result = subprocess.run(["bash", "-c", summary_step["run"]], cwd=root,
                            env={**os.environ, "INSTALL_OUTCOME": install, "SCAN_OUTCOME": scan,
                                 "RESULT_FILE": str(fresh), "GITHUB_STEP_SUMMARY": str(summary)},
                            capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn(expected, summary.read_text())


if __name__ == "__main__":
    unittest.main()
