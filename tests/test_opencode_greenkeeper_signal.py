"""The OpenCode greenkeeper signal dispatches one identifier and stays green."""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/reusable-opencode-greenkeeper-signal.yml"


def load():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def dispatch_script():
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"python3 - <<'PY'\n(.*)\n          PY\n", text, re.DOTALL)
    if match is None:
        raise AssertionError("dispatch script is missing")
    return textwrap.dedent(match.group(1))


class SignalContractTests(unittest.TestCase):
    def test_call_contract(self):
        data = load()
        call = data["on"]["workflow_call"]
        runner = call["inputs"]["runner-json"]
        self.assertEqual(runner["type"], "string")
        self.assertEqual(runner["default"], "")
        self.assertIn("PRIVATE_REPO_SELF_HOSTED=true", runner["description"])
        self.assertFalse(call["secrets"]["app_private_key"]["required"])
        self.assertEqual(data["permissions"], {"contents": "read"})
        job = data["jobs"]["signal"]
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertIn('["ubuntu-latest"]', job["runs-on"])
        self.assertIn("runner-keep-hosted", WORKFLOW.read_text(encoding="utf-8"))
        names = [step["name"] for step in job["steps"]]
        self.assertEqual(names, [
            "Mint the dispatch token",
            "Note a missing dispatch token",
            "Dispatch the private controller",
        ])
        note = job["steps"][1]
        self.assertEqual(note["if"], "steps.token.outcome != 'success'")
        self.assertNotIn("secrets", note["run"])
        self.assertNotIn("checkout", WORKFLOW.read_text(encoding="utf-8"))
        for step in job["steps"]:
            self.assertTrue(step["continue-on-error"])
            self.assertEqual(step["timeout-minutes"], 2)
            self.assertLess(step["timeout-minutes"], job["timeout-minutes"])
        token = job["steps"][0]["with"]
        self.assertEqual(token["repositories"], ".github-private")
        self.assertEqual(token["owner"], "seankoji-com")
        dispatch = job["steps"][2]
        self.assertEqual(dispatch["if"], "steps.token.outcome == 'success'")
        self.assertNotIn("echo ", dispatch["run"])

    def test_invalid_events_do_not_call_gh(self):
        script = dispatch_script()
        cases = [
            {"PR_NUMBER": "nope", "HEAD_SHA": "abcd", "REPOSITORY": "seankoji-com/example"},
            {"PR_NUMBER": "0", "HEAD_SHA": "a" * 40, "REPOSITORY": "seankoji-com/example"},
            {"PR_NUMBER": "7", "HEAD_SHA": "A" * 40, "REPOSITORY": "seankoji-com/example"},
            {"PR_NUMBER": "7", "HEAD_SHA": "a" * 40, "REPOSITORY": "../example"},
            {"PR_NUMBER": "7", "HEAD_SHA": "a" * 40, "REPOSITORY": "evil/example"},
            {"PR_NUMBER": "7", "HEAD_SHA": "a" * 40, "REPOSITORY": "seankoji-com/.."},
        ]
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    fake = Path(tmp) / "gh"
                    fake.write_text("#!/bin/sh\necho called > \"$GH_CALLED\"\nexit 0\n", encoding="utf-8")
                    fake.chmod(0o755)
                    called = Path(tmp) / "called"
                    env = {
                        **os.environ,
                        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
                        "GH_CALLED": str(called),
                        "GH_TOKEN": "sekret",
                        **case,
                    }
                    result = subprocess.run(
                        ["python3", "-c", script], text=True, capture_output=True, env=env,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(called.exists())
                    self.assertIn("::warning::greenkeeper signal skipped", result.stdout)
                    self.assertNotIn("sekret", result.stdout + result.stderr)

    def test_missing_pull_request_stays_quiet(self):
        script = dispatch_script()
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "gh"
            fake.write_text("#!/bin/sh\necho called > \"$GH_CALLED\"\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            called = Path(tmp) / "called"
            env = {
                **os.environ,
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
                "GH_CALLED": str(called),
                "GH_TOKEN": "sekret",
                "PR_NUMBER": "",
                "HEAD_SHA": "a" * 40,
                "REPOSITORY": "seankoji-com/example",
            }
            result = subprocess.run(
                ["python3", "-c", script], text=True, capture_output=True, env=env,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(called.exists())
        self.assertNotIn("::warning::", result.stdout)
        self.assertNotIn("sekret", result.stdout + result.stderr)

    def test_valid_event_posts_only_the_dispatch_payload(self):
        script = dispatch_script()
        cases = [
            ("pull_request_review", "review"),
            ("workflow_run", "checks"),
            ("pull_request_target", "head"),
        ]
        for event_name, kind in cases:
            with self.subTest(event=event_name):
                with tempfile.TemporaryDirectory() as tmp:
                    fake = Path(tmp) / "gh"
                    fake.write_text(
                        "#!/bin/sh\n"
                        "printf '%s\\n' \"$@\" > \"$GH_ARGS\"\n"
                        "prev=\n"
                        "for arg in \"$@\"; do\n"
                        "  if [ \"$prev\" = \"--input\" ]; then cp \"$arg\" \"$GH_PAYLOAD\"; fi\n"
                        "  prev=$arg\n"
                        "done\n"
                        "exit 0\n",
                        encoding="utf-8",
                    )
                    fake.chmod(0o755)
                    args = Path(tmp) / "args"
                    copied = Path(tmp) / "payload.json"
                    env = {
                        **os.environ,
                        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
                        "GH_ARGS": str(args),
                        "GH_PAYLOAD": str(copied),
                        "RUNNER_TEMP": tmp,
                        "PR_NUMBER": "7",
                        "HEAD_SHA": "a" * 40,
                        "REPOSITORY": "seankoji-com/example",
                        "EVENT_NAME": event_name,
                        "ACTOR": "octocat",
                        "DELIVERY_ID": f"1-1-{event_name}",
                        "GH_TOKEN": "sekret",
                    }
                    result = subprocess.run(
                        ["python3", "-c", script], text=True, capture_output=True, env=env,
                    )
                    recorded = args.read_text(encoding="utf-8")
                    payload = json.loads(copied.read_text(encoding="utf-8"))
                    self.assertFalse((Path(tmp) / "greenkeeper-dispatch.json").exists())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("repos/seankoji-com/.github-private/dispatches", recorded)
                self.assertIn("--method\nPOST", recorded)
                self.assertNotIn("sekret", recorded)
                self.assertNotIn("sekret", result.stdout + result.stderr)
                self.assertNotIn("::warning::", result.stdout)
                self.assertEqual(payload, {
                    "event_type": "opencode-greenkeeper-requested",
                    "client_payload": {
                        "repository": "seankoji-com/example",
                        "number": 7,
                        "head_sha": "a" * 40,
                        "kind": kind,
                        "delivery_id": f"1-1-{event_name}",
                        "actor": "octocat",
                    },
                })

    def test_dispatch_failure_removes_the_payload(self):
        script = dispatch_script()
        reasons = {
            "missing": "could not start gh",
            "stall": "timed out",
            "unwritable": "could not write the payload",
        }
        for name, expected in reasons.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    called = Path(tmp) / "called"
                    runner = tmp
                    if name == "stall":
                        fake = Path(tmp) / "gh"
                        fake.write_text(
                            f"#!{sys.executable}\nimport time\ntime.sleep(30)\n",
                            encoding="utf-8",
                        )
                        fake.chmod(0o755)
                        env_path = tmp
                    else:
                        env_path = str(Path(tmp) / "empty")
                        Path(env_path).mkdir()
                        if name == "unwritable":
                            runner = str(Path(tmp) / "not-a-directory")
                            Path(runner).write_text("x", encoding="utf-8")
                    env = {
                        **os.environ,
                        "PATH": env_path,
                        "RUNNER_TEMP": runner,
                        "GH_CALLED": str(called),
                        "DISPATCH_TIMEOUT_SECONDS": "1",
                        "PR_NUMBER": "7",
                        "HEAD_SHA": "a" * 40,
                        "REPOSITORY": "seankoji-com/example",
                        "EVENT_NAME": "workflow_run",
                        "ACTOR": "octocat",
                        "DELIVERY_ID": "9-1-workflow_run",
                        "GH_TOKEN": "sekret",
                    }
                    result = subprocess.run(
                        [sys.executable, "-c", script], text=True, capture_output=True, env=env,
                    )
                    self.assertFalse((Path(tmp) / "greenkeeper-dispatch.json").exists())
                    if name != "stall":
                        self.assertFalse(called.exists())
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"::warning::greenkeeper dispatch failed ({expected})", result.stdout)
                self.assertNotIn("sekret", result.stdout + result.stderr)
