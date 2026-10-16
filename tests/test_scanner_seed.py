"""Offline seed fixtures: real pip, synthetic scanner wheels, no real scans."""

import base64
import gzip
import hashlib
import importlib.util
import io
from itertools import product
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from contextlib import redirect_stdout

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/scanner_seed.py"
spec = importlib.util.spec_from_file_location("scanner_seed", SOURCE)
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
WORKFLOW = ROOT / ".github/workflows/reusable-static-analysis.yml"
PINS = yaml.safe_load(WORKFLOW.read_text())["env"]


def condition(expression, values):
    expression = expression.replace("&&", " and ")
    for name in sorted(values, key=len, reverse=True):
        expression = expression.replace(name, repr(values[name]))
    return eval(expression, {"__builtins__": {}, "format": lambda s, v: s.format(v)})


def wheel(directory, name, version, *, dependencies=(), scanner=False, wrong_version=False, pip_source=None):
    """Write a valid pure-Python wheel without build tools/network."""
    module = name.replace("-", "_")
    info = f"{module}-{version}.dist-info"
    files = {
        f"{info}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                              + "".join(f"Requires-Dist: {d}\n" for d in dependencies)).encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{module}/__init__.py": b"",
    }
    if pip_source:
        for path in pip_source.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files["pip/" + path.relative_to(pip_source).as_posix()] = path.read_bytes()
        # The fixture uses real pip code and pins its distribution and CLI version.
        files["pip/__init__.py"] = re.sub(rb'__version__ = "[^"]+"',
                                          f'__version__ = "{version}"'.encode(), files["pip/__init__.py"])
    if scanner:
        shown = "0.0.0" if wrong_version else version
        files[f"{module}/__init__.py"] = f'def main():\n    print("{name} {shown}")\n'.encode()
        files[f"{info}/entry_points.txt"] = f"[console_scripts]\n{name} = {module}:main\n".encode()
    files[f"{info}/RECORD"] = "".join(f"{p},,\n" for p in [*files, f"{info}/RECORD"]).encode()
    path = directory / f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return path


class SeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scanner-fixture-")
        cls.root = Path(cls.temp.name)
        # Copy an already installed pip; there are no downloads or host installs.
        bootstrap = cls.root / "bootstrap"
        subprocess.run([sys.executable, "-I", "-m", "venv", str(bootstrap)], check=True, timeout=30)
        cls.python = bootstrap / "bin/python"
        result = subprocess.run([str(cls.python), "-I", "-c", "import pip; print(pip.__path__[0])"],
                                check=True, capture_output=True, text=True)
        cls.pip_source = Path(result.stdout.strip())
        cls.base = cls.root / "base"
        cls.base.mkdir()
        wheel(cls.base, "pip", PINS["PIN_PIP"], pip_source=cls.pip_source)
        wheel(cls.base, "fixture-dep", "2.0")
        for tool in ("semgrep", "zizmor"):
            house = cls.root / (tool + "-house")
            shutil.copytree(cls.base, house)
            wheel(house, tool, PINS["PIN_" + tool.upper()], dependencies=("fixture-dep==2.0",), scanner=True)
            if tool == "semgrep":
                for name in ("requests", "threading"):
                    wheel(house, "opentelemetry-instrumentation-" + name, PINS["PIN_SEMGREP_OTEL"])
            output = cls.root / (tool + "-seed")
            with patch.dict(os.environ, PINS), patch.object(seed.platform, "system", return_value="Linux"), \
                 patch.object(seed.sys, "executable", str(cls.python)):
                seed.build(tool, house, output)
            evidence = os.environ.get("SCANNER_TEST_EVIDENCE")
            if evidence:
                Path(evidence, f"scanner-fixture-{tool}-manifest.json").write_text((output / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="scanner-consume-")
        self.addCleanup(self.work.cleanup)
        # 3.11+ canonicalises a symlinked temp root when it computes the venv
        # sys.prefix (macOS /var -> /private/var), so keep fixtures off the
        # unresolved path the production interpreter would report.
        self.root = Path(self.work.name).resolve()
        self.start_env = patch.dict(os.environ, {**PINS, "PIP_CONFIG_FILE": os.devnull,
                                               "PIP_CACHE_DIR": str(self.root / "pip-cache"),
                                               "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        self.start_env.start()
        self.addCleanup(self.start_env.stop)
        self.start_os = patch.object(seed.platform, "system", return_value="Linux")
        self.start_os.start()
        self.addCleanup(self.start_os.stop)

    def fixture(self, tool="zizmor", *, root=None):
        path = (root or self.root) / "seed"
        shutil.copytree(self.__class__.root / (tool + "-seed"), path)
        for entry in [path, *path.rglob("*")]:
            entry.chmod(0o555 if entry.is_dir() else 0o444)
        # Unit tests cannot chown root or make /tmp's ancestors immutable. Model
        # only that image trust boundary here; test every production stat check
        # independently below. File types/modes/content are otherwise real.
        def trusted_fixture(entry, *, directory=False, ancestor=False):
            if ancestor:
                return
            info = entry.lstat()
            info = type("RootStat", (), {"st_uid": 0, "st_mode": info.st_mode})()
            with patch.object(Path, "lstat", return_value=info):
                seed.immutable_original(entry, directory=directory)
        return path, trusted_fixture

    def venv(self):
        path = self.root / "job-venv"
        subprocess.run([sys.executable, "-I", "-m", "venv", "--clear", str(path)],
                       check=True, timeout=30, capture_output=True)
        return path

    def workflow_venv(self, path, layout):
        # Both alias directions must keep executing a real venv interpreter.
        # The optional uv executable exercises its actual offline creation too;
        # CI needs no additional dependency to reproduce its alias topology.
        uv = os.environ.get("SCANNER_TEST_UV")
        if layout == "uv" and uv:
            subprocess.run([uv, "venv", "--offline", "--no-cache", "--no-config",
                            "--no-python-downloads", "--no-managed-python",
                            "--python", sys.executable, str(path)],
                           check=True, timeout=30, capture_output=True)
            subprocess.run([str(path / "bin/python"), "-I", "-m", "ensurepip"],
                           check=True, timeout=30, capture_output=True)
        else:
            subprocess.run([str(self.__class__.python.with_name("python3")), "-I", "-m",
                            "venv", str(path)], check=True, timeout=30, capture_output=True)
            if layout == "uv":
                interpreter = (path / "bin/python3").resolve()
                for alias in (path / "bin").glob("python*"):
                    alias.unlink()
                (path / "bin/python").symlink_to(interpreter)
                (path / "bin/python3").symlink_to("python")
                (path / f"bin/python3.{sys.version_info.minor}").symlink_to("python")
        # Native alias topology is version-dependent: a 3.12 venv links
        # bin/python -> python3.12 (not -> python3), while uv links
        # bin/python3 -> python and bin/python -> the host interpreter. Both
        # directions still execute a real venv interpreter below.
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        alias = path / ("bin/python" if layout == "venv" else "bin/python3")
        expected = {"python3", version} if layout == "venv" else {"python"}
        self.assertIn(os.readlink(alias), expected, "Unexpected native venv alias topology")
        result = subprocess.run([str(path / "bin/python3"), "-I", "-c",
                                 "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                check=True, timeout=10, capture_output=True, text=True)
        return Path(result.stdout.strip())

    def consume(self, path, trust, tool="zizmor"):
        with patch.object(seed, "seed_path", return_value=path), patch.object(seed, "immutable", side_effect=trust):
            seed.install(tool, self.venv())

    def rewrite(self, path, change):
        target = path / "manifest.json"
        manifest = json.loads(target.read_text())
        change(manifest)
        target.chmod(0o644)
        target.write_text(json.dumps(manifest))
        target.chmod(0o444)

    def remove_fixture(self, path):
        for entry in [path, *path.rglob("*")]:
            if not entry.is_symlink() and entry.is_dir():
                entry.chmod(0o755)
        shutil.rmtree(path)

    def test_valid_offline_seed_real_pip_both_tools(self):
        for tool in ("semgrep", "zizmor"):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "seed"
                original, trust = self.fixture(tool)
                shutil.copytree(original, path)
                # Trap attempted network activity as a subprocess requirement:
                # every installation must be no-index; exact closure is verified
                # by real pip and importlib.metadata in a fresh cleared venv.
                real = seed.pip_command
                calls = []
                def run(python, *args):
                    calls.append(args)
                    if args[0] == "install":
                        self.assertIn("--no-index", args)
                        self.assertIn("--require-hashes", args)
                    return real(python, *args)
                with patch.object(seed, "pip_command", side_effect=run), \
                     patch.dict(os.environ, {"PIP_FIND_LINKS": "https://example.invalid/ambient/",
                                             "PIP_TARGET": str(self.root / "ambient-target")}):
                    self.consume(path, trust, tool)
                self.assertFalse((self.root / "ambient-target").exists())
                self.assertEqual([c[0] for c in calls], ["uninstall", "install", "check"])
                self.remove_fixture(original)

    def test_probe_is_read_only_and_only_publishes_verified_modes(self):
        for tool in ("semgrep", "zizmor"):
            path, trust = self.fixture(tool)
            before = {p.relative_to(path): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in path.rglob("*") if p.is_file()}
            with patch.object(seed, "seed_path", return_value=path), \
                 patch.object(seed, "immutable", side_effect=trust), \
                 patch.object(seed.subprocess, "run") as run, \
                 patch.object(seed.subprocess, "check_output") as scanner, \
                 redirect_stdout(io.StringIO()) as output:
                seed.probe(tool)
            self.assertEqual(output.getvalue(), "cache_mode=offline\n")
            run.assert_not_called()
            scanner.assert_not_called()
            self.assertEqual(before, {p.relative_to(path): (p.read_bytes(), p.stat().st_mtime_ns)
                                      for p in path.rglob("*") if p.is_file()})
            self.remove_fixture(path)
        with patch.object(seed, "seed_path", return_value=self.root / "absent"), \
             redirect_stdout(io.StringIO()) as output:
            seed.probe("zizmor")
        self.assertEqual(output.getvalue(), "cache_mode=fallback\n")

    def test_literal_workflow_offline_fallback_and_corrupt_paths(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        for tool, job in jobs.items():
            steps = job["steps"]
            probe = next(s for s in steps if s.get("id") == "seed")
            cache = next(s for s in steps if s.get("id") == "cache")
            install = next(s for s in steps if s.get("id") == "install")
            scan = next(s for s in steps if s.get("id") == "scan")
            save = next(s for s in steps if "actions/cache/save@" in s.get("uses", ""))
            summary = next(s for s in steps if s["name"] == "Record scan outcome")
            self.assertLess(steps.index(probe), steps.index(cache))
            self.assertLess(steps.index(cache), steps.index(install))
            self.assertEqual(probe["if"], "steps.python.outcome == 'success'")
            self.assertEqual(cache["if"], "steps.seed.outcome == 'success' && steps.seed.outputs.cache_mode == 'fallback'")
            self.assertEqual(install["if"], "steps.python.outcome == 'success' && steps.seed.outcome == 'success'")
            for layout, case in product(("venv", "uv"),
                                        ("offline", "missing", "abi-mismatch", "pin-mismatch", "corrupt")):
                with self.subTest(tool=tool, layout=layout, case=case), \
                     tempfile.TemporaryDirectory(dir=self.root) as temp:
                    root = Path(temp)
                    path, trust = self.fixture(tool, root=root)
                    if case == "missing":
                        self.remove_fixture(path)
                    elif case == "abi-mismatch":
                        self.rewrite(path, lambda m: m["identity"].update(abi="mismatch"))
                    elif case == "pin-mismatch":
                        self.rewrite(path, lambda m: m["identity"]["pins"].update(PIN_PIP="0.0"))
                    elif case == "corrupt":
                        self.rewrite(path, lambda m: m.update(wheels=[]))
                    job_venv = root / (tool + "-venv")
                    site = self.workflow_venv(job_venv, layout)
                    interpreters = {name: (job_venv / "bin" / name).resolve()
                                    for name in ("python", "python3")}
                    # Execute the literal workflow with intact venv interpreters.
                    # A site-packages .pth import runs even under -I and even
                    # when the runner's stdlib ships its own sitecustomize; the
                    # boot module stubs only image trust and downloads when the
                    # embedded helper executes.
                    (site / "_scanner_fixture_boot.py").write_text('''import builtins, json, os, subprocess
from pathlib import Path
from unittest.mock import patch
original_exec = builtins.exec
def fixture_exec(code, namespace=None, *args, **kwargs):
    if getattr(code, "co_filename", None) != "scanner_seed.py":
        return original_exec(code, namespace, *args, **kwargs)
    namespace["__name__"] = "fixture_module"
    original_exec(code, namespace)
    namespace["seed_path"] = lambda wanted: Path(os.environ["FIXTURE_SEED"])
    original_stat = namespace["immutable"]
    def trust(path, *, directory=False, ancestor=False):
        if ancestor:
            return
        info = path.lstat()
        info = type("RootStat", (), {"st_uid": 0, "st_mode": info.st_mode})()
        with patch.object(Path, "lstat", return_value=info):
            original_stat(path, directory=directory)
    namespace["immutable"] = trust
    namespace["platform"].system = lambda: "Linux"
    original_pip = namespace["pip_command"]
    def pip(python, *args):
        with open(os.environ["FIXTURE_PIP_LOG"], "a") as log:
            log.write(json.dumps(args) + "\\n")
        if args[0] == "install" and "--no-index" not in args:
            raise subprocess.CalledProcessError(17, ["fixture-download-trap"])
        return original_pip(python, *args)
    namespace["pip_command"] = pip
    try:
        namespace["main"]()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode)
builtins.exec = fixture_exec
''')
                    (site / "zz-scanner-fixture-boot.pth").write_text("import _scanner_fixture_boot\n")
                    output, github_path, pip_log = (root / name for name in ("output", "github-path", "pip-calls"))
                    for p in (output, github_path, pip_log):
                        p.unlink(missing_ok=True)
                    env = {**os.environ, **PINS, "RUNNER_TEMP": str(root),
                           "GITHUB_OUTPUT": str(output), "GITHUB_PATH": str(github_path),
                           "FIXTURE_SEED": str(path), "FIXTURE_PIP_LOG": str(pip_log)}
                    result = subprocess.run(["/bin/bash", "-c", probe["run"]], env=env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertFalse(pip_log.exists(), "Probe must never run pip")
                    self.assertFalse(github_path.exists())
                    mode = "offline" if case == "offline" else "fallback"
                    if case == "corrupt":
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(output.exists(), "Corrupt seed must not publish a mode")
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(output.read_text(), f"cache_mode={mode}\n")
                    values = {"steps.python.outcome": "success",
                              "steps.seed.outcome": "failure" if case == "corrupt" else "success",
                              "steps.seed.outputs.cache_mode": "" if case == "corrupt" else mode}
                    self.assertEqual(condition(cache["if"], values), case in {"missing", "abi-mismatch", "pin-mismatch"})
                    if condition(install["if"], values):
                        result = subprocess.run(["/bin/bash", "-c", install["run"]], env=env,
                                                capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0 if case == "offline" else 17, result.stderr)
                        values["steps.install.outcome"] = "success" if result.returncode == 0 else "failure"
                    else:
                        values["steps.install.outcome"] = "skipped"
                    self.assertEqual(condition(scan["if"], values), case == "offline")
                    self.assertEqual(github_path.exists(), case == "offline")
                    if case == "offline":
                        calls = [json.loads(line) for line in pip_log.read_text().splitlines()]
                        self.assertEqual([c[0] for c in calls], ["uninstall", "install", "check"])
                        self.assertIn("--no-index", calls[1])
                        self.assertIn("--require-hashes", calls[1])
                    elif case != "corrupt":
                        call = json.loads(pip_log.read_text())
                        self.assertIn("--timeout", call)
                        self.assertIn("--retries", call)
                    else:
                        self.assertFalse(pip_log.exists())
                        report = root / "summary"
                        report.unlink(missing_ok=True)
                        result = subprocess.run(["/bin/bash", "-c", summary["run"]],
                            env={**env, "INSTALL_OUTCOME": "skipped", "SCAN_OUTCOME": "skipped",
                                 "RESULT_FILE": "", "GITHUB_STEP_SUMMARY": str(report)},
                            capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("NO-OUTPUT", report.read_text())
                        self.assertNotIn("SCANNED:", report.read_text())
                    # Even hypothetical success cannot save downloads on offline.
                    values.update({"success()": True, "github.event_name": "push", "github.ref": "refs/heads/main",
                                   "github.event.repository.default_branch": "main", "steps.scan.outcome": "success",
                                   "steps.cache.outputs.cache-hit": "false", "steps.cache.outputs.cache-primary-key": "key"})
                    self.assertFalse(condition(save["if"], values))
                    for name, interpreter in interpreters.items():
                        self.assertEqual((job_venv / "bin" / name).resolve(), interpreter)
                        self.assertEqual(subprocess.check_output(
                            [str(job_venv / "bin" / name), "-I", "-c", "import sys; print(sys.prefix)"],
                            env=env, text=True, timeout=10).strip(), str(job_venv))
                    if path.exists():
                        self.remove_fixture(path)

    def test_pin_abi_platform_arch_tool_mismatch_retains_bounded_fallback(self):
        for field in ("pins", "abi", "os", "arch", "tool"):
            with self.subTest(field=field):
                path, trust = self.fixture()
                self.rewrite(path, lambda m: m["identity"].update(
                    {field: {**PINS, "PIN_PIP": "0.0"} if field == "pins" else "mismatch"}))
                with patch.object(seed, "seed_path", return_value=path), \
                     patch.object(seed, "immutable", side_effect=trust), redirect_stdout(io.StringIO()) as output:
                    seed.probe("zizmor")
                self.assertEqual(output.getvalue(), "cache_mode=fallback\n")
                calls = []
                with patch.object(seed, "seed_path", return_value=path), patch.object(seed, "immutable", side_effect=trust), \
                     patch.object(seed, "pip_command", side_effect=lambda py, *a: calls.append(a)), \
                     patch.object(seed.subprocess, "run"), patch.object(seed.subprocess, "check_output", return_value="zizmor " + PINS["PIN_ZIZMOR"]), \
                     patch.object(Path, "is_file", return_value=True):
                    seed.install("zizmor", self.root / "venv")
                self.assertEqual([c[0] for c in calls], ["install", "install", "check"])
                for call in calls[:2]:
                    self.assertIn("--timeout", call)
                    self.assertIn("--retries", call)
                    self.assertIn("--only-binary=:all:", call)
                    self.assertNotIn("--no-index", call)
                self.assertIn("pip==" + PINS["PIN_PIP"], calls[0])
                self.assertIn("zizmor==" + PINS["PIN_ZIZMOR"], calls[1])
                self.remove_fixture(path)

    def test_absent_seed_fallback_and_failed_download_propagate(self):
        with patch.object(seed, "seed_path", return_value=self.root / "absent"), \
             patch.object(seed.subprocess, "run"), \
             patch.object(seed, "pip_command", side_effect=subprocess.CalledProcessError(17, ["pip"])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                seed.install("zizmor", self.root / "venv")
        self.assertEqual(run.call_count, 1)

    def test_corrupt_matching_seed_never_installs_or_falls_back(self):
        for case in ("empty", "duplicate", "missing", "extra", "bytes", "requirements", "wrong-pin", "symlink", "writable"):
            with self.subTest(case=case):
                path, trust = self.fixture()
                wheels = path / "wheels"
                first = next(wheels.iterdir())
                if case == "empty":
                    self.rewrite(path, lambda m: m.update(wheels=[]))
                elif case == "duplicate":
                    self.rewrite(path, lambda m: m["wheels"].append(m["wheels"][0]))
                elif case == "wrong-pin":
                    self.rewrite(path, lambda m: m.update(wheels=[r for r in m["wheels"] if r["name"] != "pip"]))
                elif case == "missing":
                    wheels.chmod(0o755)
                    first.unlink()
                    wheels.chmod(0o555)
                elif case == "extra":
                    wheels.chmod(0o755)
                    (wheels / "extra.whl").write_bytes(first.read_bytes())
                    wheels.chmod(0o555)
                elif case in {"bytes", "writable"}:
                    first.chmod(0o644)
                    if case == "bytes":
                        first.write_bytes(first.read_bytes() + b"changed")
                        first.chmod(0o444)
                elif case == "requirements":
                    (path / "requirements.txt").chmod(0o644)
                    (path / "requirements.txt").write_text("")
                    (path / "requirements.txt").chmod(0o444)
                elif case == "symlink":
                    wheels.chmod(0o755)
                    target = self.root / "target"
                    target.write_bytes(first.read_bytes())
                    first.unlink()
                    first.symlink_to(target)
                    wheels.chmod(0o555)
                with patch.object(seed, "pip_command") as run, \
                     patch.object(seed, "seed_path", return_value=path), patch.object(seed, "immutable", side_effect=trust):
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        seed.install("zizmor", self.root / "venv")
                    with redirect_stdout(io.StringIO()) as output:
                        with self.assertRaises((ValueError, FileNotFoundError)):
                            seed.probe("zizmor")
                    self.assertEqual(output.getvalue(), "")
                run.assert_not_called()
                self.remove_fixture(path)

    def test_incomplete_dependency_closure_fails_real_offline_install(self):
        path, trust = self.fixture()
        manifest = json.loads((path / "manifest.json").read_text())
        removed = next(r for r in manifest["wheels"] if r["name"] == "fixture-dep")
        self.rewrite(path, lambda m: m.update(wheels=[r for r in m["wheels"] if r != removed]))
        (path / "wheels").chmod(0o755)
        (path / "wheels" / removed["file"]).unlink()
        (path / "wheels").chmod(0o555)
        req = path / "requirements.txt"
        req.chmod(0o644)
        req.write_text(seed.requirements([r for r in manifest["wheels"] if r != removed]))
        req.chmod(0o444)
        with patch.object(seed.subprocess, "run") as run, \
             patch.object(seed, "seed_path", return_value=path), \
             patch.object(seed, "immutable", side_effect=trust), redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError, "dependency closure"):
                seed.probe("zizmor")
        run.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        with self.assertRaisesRegex(ValueError, "dependency closure"):
            self.consume(path, trust)

    def test_build_rejects_empty_extra_duplicate_incomplete_and_url_wheels(self):
        for case in ("empty", "extra", "duplicate", "incomplete", "url"):
            with self.subTest(case=case):
                house = self.root / case
                shutil.copytree(self.__class__.root / "zizmor-house", house)
                if case == "empty":
                    shutil.rmtree(house)
                    house.mkdir()
                elif case == "extra":
                    wheel(house, "extra", "1.0")
                elif case == "duplicate":
                    wheel(house, "fixture-dep", "3.0")
                elif case == "incomplete":
                    next(house.glob("fixture_dep-*.whl")).unlink()
                else:
                    wheel(house, "extra", "1.0", dependencies=("remote @ https://example.invalid/remote.whl",))
                with self.assertRaises((ValueError, subprocess.CalledProcessError)), \
                     patch.object(seed.sys, "executable", str(self.python)):
                    seed.build("zizmor", house, self.root / (case + "-out"))

    def test_probe_rejects_hashed_but_incompatible_or_unresolved_wheels(self):
        for case in ("version", "extra", "wheel-abi", "unrelated"):
            with self.subTest(case=case):
                path, trust = self.fixture()
                wheels = path / "wheels"
                wheels.chmod(0o755)
                if case in {"version", "extra"}:
                    scanner = next(wheels.glob("zizmor-*.whl"))
                    scanner.unlink()
                    wheel(wheels, "zizmor", PINS["PIN_ZIZMOR"], scanner=True,
                          dependencies=("fixture-dep==3.0" if case == "version" else "fixture-dep[enabled]==2.0",))
                    if case == "extra":
                        next(wheels.glob("fixture_dep-*.whl")).unlink()
                        wheel(wheels, "fixture-dep", "2.0", dependencies=('missing==1.0; extra == "enabled"',))
                elif case == "wheel-abi":
                    scanner = next(wheels.glob("zizmor-*.whl"))
                    scanner.rename(wheels / scanner.name.replace("py3-none-any", "cp999-cp999-unknown"))
                else:
                    wheel(wheels, "unrelated", "1.0")
                records = sorted((seed.wheel_record(p) for p in wheels.iterdir()), key=lambda r: r["name"])
                self.rewrite(path, lambda m: m.update(wheels=records))
                req = path / "requirements.txt"
                req.chmod(0o644)
                req.write_text(seed.requirements(records))
                for p in [wheels, *wheels.iterdir(), req]:
                    p.chmod(0o555 if p.is_dir() else 0o444)
                with patch.object(seed, "seed_path", return_value=path), \
                     patch.object(seed, "immutable", side_effect=trust), \
                     patch.object(seed.subprocess, "run") as run, redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(ValueError):
                        seed.probe("zizmor")
                run.assert_not_called()
                self.assertEqual(output.getvalue(), "")
                self.remove_fixture(path)

    def test_stat_boundary_rejects_nonroot_writable_and_symlink_files_and_parents(self):
        for uid, mode, directory, ancestor in (
            (1, stat.S_IFREG | 0o444, False, False),
            (0, stat.S_IFREG | 0o644, False, False),
            (0, stat.S_IFLNK | 0o444, False, False),
            (0, stat.S_IFDIR | 0o755, True, False),
            (0, stat.S_IFDIR | 0o777, True, True),
            (1, stat.S_IFDIR | 0o755, True, True),
            (0, stat.S_IFLNK | 0o755, True, True),
        ):
            with self.subTest(uid=uid, mode=mode, ancestor=ancestor):
                info = type("Stat", (), {"st_uid": uid, "st_mode": mode})()
                with patch.object(Path, "lstat", return_value=info):
                    with self.assertRaises(ValueError):
                        seed.immutable(Path("/seed"), directory=directory, ancestor=ancestor)

    def test_no_ambient_scanner_or_venv_is_accepted(self):
        path, trust = self.fixture()
        with patch.object(seed, "seed_path", return_value=path), patch.object(seed, "immutable", side_effect=trust), \
             patch.object(seed, "pip_command"), patch.object(seed.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["version-check"])), \
             patch.object(seed.subprocess, "check_output") as scanner:
            with self.assertRaises(subprocess.CalledProcessError):
                seed.install("zizmor", self.root / "venv")
        scanner.assert_not_called()

    def test_system_interpreter_is_rejected_before_any_pip(self):
        path, trust = self.fixture()
        venv = self.root / "ambient-venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to(sys.executable)
        with patch.object(seed, "seed_path", return_value=path), patch.object(seed, "immutable", side_effect=trust), \
             patch.object(seed, "pip_command") as pip:
            with self.assertRaises(subprocess.CalledProcessError):
                seed.install("zizmor", venv)
        pip.assert_not_called()

    def test_wrong_scanner_version_fails_after_real_offline_install(self):
        path, trust = self.fixture()
        house = self.root / "wrong-version-house"
        shutil.copytree(self.__class__.root / "zizmor-house", house)
        wheel(house, "zizmor", PINS["PIN_ZIZMOR"], dependencies=("fixture-dep==2.0",),
              scanner=True, wrong_version=True)
        self.remove_fixture(path)
        with patch.object(seed.sys, "executable", str(self.python)):
            seed.build("zizmor", house, path)
        for entry in [path, *path.rglob("*")]:
            entry.chmod(0o555 if entry.is_dir() else 0o444)
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            self.consume(path, trust)

    def test_duplicate_manifest_keys_path_escape_and_seed_symlink_fail(self):
        path, trust = self.fixture()
        self.rewrite(path, lambda m: m["wheels"][0].update(file="../outside.whl"))
        with patch.object(seed, "immutable", side_effect=trust):
            with self.assertRaisesRegex(ValueError, "Invalid wheel path"):
                seed.verify(path, seed.identity("zizmor"))
        self.remove_fixture(path)
        path, trust = self.fixture()
        manifest = path / "manifest.json"
        manifest.chmod(0o644)
        manifest.write_text('{"schema_version": 1, "schema_version": 1}')
        manifest.chmod(0o444)
        with patch.object(seed, "immutable", side_effect=trust):
            with self.assertRaisesRegex(ValueError, "Duplicate manifest field"):
                seed.verify(path, seed.identity("zizmor"))
        link = self.root / "linked-seed"
        link.symlink_to(path)
        with patch.object(seed, "immutable", side_effect=trust):
            with self.assertRaises(ValueError):
                seed.verify(link, seed.identity("zizmor"))


    def test_missing_pin_names_are_reported_without_disclosing_values(self):
        # A documented manual build/install can hit an unset pin; it must be the
        # module's controlled ValueError, naming the variable only.
        env = {name: value for name, value in PINS.items() if name != "PIN_ZIZMOR"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "PIN_ZIZMOR") as caught:
                seed.pins()
        message = str(caught.exception)
        self.assertNotIn("KeyError", message)
        for value in env.values():
            self.assertNotIn(value, message)
        with patch.dict(os.environ, PINS):
            self.assertEqual(seed.pins(), PINS)

    def test_empty_or_malformed_pins_are_rejected(self):
        for value in ("", "1.2 beta", "v1.2", "1.2\n3", "1.2;rm"):
            with self.subTest(value=value), patch.dict(os.environ, {**PINS, "PIN_PIP": value}):
                with self.assertRaisesRegex(ValueError, "Invalid scanner pins"):
                    seed.pins()

    def test_partially_provisioned_seed_is_a_controlled_error(self):
        # Seed directory present, manifest absent: must be ValueError, never a
        # raw FileNotFoundError from lstat, so probe/install exit cleanly.
        path, trust = self.fixture()
        (path / "manifest.json").chmod(0o644)
        path.chmod(0o755)
        (path / "manifest.json").unlink()
        path.chmod(0o555)
        with patch.object(seed, "seed_path", return_value=path), \
             patch.object(seed, "immutable", side_effect=trust), \
             patch.object(seed, "pip_command") as pip, \
             patch.object(seed.subprocess, "run") as run, \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError, r"manifest") as probe_error:
                seed.probe("zizmor")
            with self.assertRaisesRegex(ValueError, r"manifest"):
                seed.install("zizmor", self.root / "venv")
        self.assertNotIsInstance(probe_error.exception, FileNotFoundError)
        self.assertEqual(output.getvalue(), "")
        run.assert_not_called()
        pip.assert_not_called()
        self.remove_fixture(path)

    def test_absent_or_symlinked_job_local_scanner_never_runs_or_publishes(self):
        # The venv/bin/<tool> guard had no coverage: earlier interpreter,
        # install and distribution checks all pass, then this branch fires.
        # The install step runs under `set -euo pipefail` and appends to
        # GITHUB_PATH only after this helper returns, so the ValueError must
        # leave stdout empty and exit non-zero: no scanner path is published.
        for case in ("absent", "symlink"):
            with self.subTest(case=case):
                path, trust = self.fixture()
                venv = self.venv()
                if case == "symlink":
                    (venv / "bin" / "zizmor").symlink_to(sys.executable)
                with patch.object(seed, "seed_path", return_value=path), \
                     patch.object(seed, "immutable", side_effect=trust), \
                     patch.object(seed, "pip_command") as pip, \
                     patch.object(seed.subprocess, "run"), \
                     patch.object(seed.subprocess, "check_output") as scanner, \
                     redirect_stdout(io.StringIO()) as output:
                    with self.assertRaisesRegex(ValueError, "Missing or symlinked job-local scanner"):
                        seed.install("zizmor", venv)
                scanner.assert_not_called()
                self.assertEqual(output.getvalue(), "", "Nothing may be published before the guard")
                self.assertEqual(
                    [call[1] for call in (c.args for c in pip.call_args_list)],
                    ["uninstall", "install", "check"],
                )
                self.remove_fixture(path)


seed.immutable_original = seed.immutable


class SourceContractTests(unittest.TestCase):
    def test_cache_save_actual_condition_only_allows_successful_default_pushes(self):
        for job in yaml.safe_load(WORKFLOW.read_text())["jobs"].values():
            save = next(s for s in job["steps"] if "actions/cache/save@" in s.get("uses", ""))
            for success, event, branch, default, installed, cache_hit, cache_key, expected in (
                (True, "push", "main", "main", "success", "false", "key", True),
                (True, "push", "trunk", "trunk", "success", "false", "key", True),
                (True, "pull_request", "main", "main", "success", "false", "key", False),
                (True, "pull_request_target", "main", "main", "success", "false", "key", False),
                (True, "workflow_dispatch", "main", "main", "success", "false", "key", False),
                (True, "push", "feature", "main", "success", "false", "key", False),
                (False, "push", "main", "main", "success", "false", "key", False),
                (True, "push", "main", "main", "failure", "false", "key", False),
                (True, "push", "main", "main", "cancelled", "false", "key", False),
                (True, "push", "main", "main", "success", "true", "key", False),
                (True, "push", "main", "main", "success", "false", "", False),
            ):
                values = {"success()": success, "github.event_name": event,
                          "github.ref": "refs/heads/" + branch,
                          "github.event.repository.default_branch": default,
                          "steps.install.outcome": installed,
                          "steps.seed.outcome": "success", "steps.seed.outputs.cache_mode": "fallback",
                          "steps.scan.outcome": "success",
                          "steps.cache.outputs.cache-hit": cache_hit,
                          "steps.cache.outputs.cache-primary-key": cache_key}
                expr = save["if"].replace("&&", " and ")
                for name in sorted(values, key=len, reverse=True):
                    expr = expr.replace(name, repr(values[name]))
                # Only this trusted workflow's limited condition grammar executes.
                self.assertEqual(eval(expr, {"__builtins__": {}, "format": lambda s, v: s.format(v)}), expected)
            for field in ("steps.scan.outcome", "steps.seed.outcome", "steps.install.outcome"):
                for outcome in ("failure", "cancelled", "skipped", ""):
                    expr = save["if"].replace("&&", " and ")
                    values.update({"success()": True, "github.event_name": "push",
                                   "github.ref": "refs/heads/main", "github.event.repository.default_branch": "main",
                                   "steps.install.outcome": "success", "steps.scan.outcome": "success",
                                   "steps.seed.outcome": "success", "steps.seed.outputs.cache_mode": "fallback",
                                   "steps.cache.outputs.cache-hit": "false", "steps.cache.outputs.cache-primary-key": "key"})
                    values[field] = outcome
                    for name in sorted(values, key=len, reverse=True):
                        expr = expr.replace(name, repr(values[name]))
                    self.assertFalse(eval(expr, {"__builtins__": {}, "format": lambda s, v: s.format(v)}))
            for mode in ("offline", "", "unknown"):
                expr = save["if"].replace("&&", " and ")
                values.update({"success()": True, "github.event_name": "push",
                               "github.ref": "refs/heads/main", "github.event.repository.default_branch": "main",
                               "steps.install.outcome": "success", "steps.scan.outcome": "success",
                               "steps.seed.outcome": "success", "steps.seed.outputs.cache_mode": mode,
                               "steps.cache.outputs.cache-hit": "false", "steps.cache.outputs.cache-primary-key": "key"})
                for name in sorted(values, key=len, reverse=True):
                    expr = expr.replace(name, repr(values[name]))
                self.assertFalse(eval(expr, {"__builtins__": {}, "format": lambda s, v: s.format(v)}))

    def test_embedded_helper_is_exact_checksum_verified_source_for_every_job(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        for job in jobs.values():
            for step in (s for s in job["steps"] if s.get("id") in {"seed", "install"}):
                encoded = re.search(r'base64.b64decode\("([A-Za-z0-9+/=]+)"\)', step["run"]).group(1)
                source = gzip.decompress(base64.b64decode(encoded))
                self.assertEqual(source, SOURCE.read_bytes())
                self.assertIn(hashlib.sha256(source).hexdigest(), step["run"])
                self.assertIn('-venv/bin/python3" -I', step["run"])
                self.assertNotIn("raw.githubusercontent", step["run"])
                self.assertNotIn("scripts/scanner_seed.py", step["run"])

    def test_install_publishes_scanner_path_only_after_a_successful_helper(self):
        # A helper that raises ValueError (missing/symlinked scanner, partial
        # seed) exits non-zero; `set -euo pipefail` then aborts the step before
        # the GITHUB_PATH append, so no path is ever published on failure.
        for job in yaml.safe_load(WORKFLOW.read_text())["jobs"].values():
            install = next(s for s in job["steps"] if s.get("id") == "install")
            run = install["run"]
            self.assertIn("set -euo pipefail", run)
            self.assertLess(run.index("exec(compile(source"), run.index("$GITHUB_PATH"))
            self.assertEqual(run.count('>> "$GITHUB_PATH"'), 1)

    def test_complete_scopes_and_current_pins_advisory_and_hosted_isolation(self):
        document = yaml.safe_load(WORKFLOW.read_text())
        self.assertEqual(document["env"], {"PIN_PIP": "25.2", "PIN_SEMGREP": "1.176.0",
                                           "PIN_SEMGREP_OTEL": "0.58b0", "PIN_ZIZMOR": "1.30.0"})
        self.assertEqual(document["permissions"], {"contents": "read"})
        self.assertEqual('["ubuntu-latest"]', document[True]["workflow_call"]["inputs"]["runner-json"]["default"])
        for tool, job in document["jobs"].items():
            self.assertEqual(job["runs-on"], "${{ fromJSON(inputs.runner-json) }}")
            scan = next(s for s in job["steps"] if s.get("id") == "scan")
            install = next(s for s in job["steps"] if s.get("id") == "install")
            self.assertTrue(install["continue-on-error"])
            self.assertTrue(scan["continue-on-error"])
            self.assertIn(f'"$RUNNER_TEMP/{tool}-venv/bin/{tool}"', scan["run"])
            self.assertNotRegex(scan["run"], r"--exclude|--include|--disable|head |grep ")
            if tool == "semgrep":
                self.assertIn("--config p/security-audit --config p/secrets", scan["run"])
                self.assertIn('--metrics=off --json --output "$RESULT_FILE" .', scan["run"])
                self.assertEqual(scan["env"]["SEMGREP_SEND_METRICS"], "off")
            else:
                self.assertIn('--no-online-audits --format json .github/workflows > "$RESULT_FILE"', scan["run"])


if __name__ == "__main__":
    unittest.main()
