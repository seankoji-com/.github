"""Build/consume scanner wheel seeds; never execute a seed's binaries.

Build with the target Python: scanner_seed.py build TOOL WHEELHOUSE OUTPUT.
Resolve all workflow pins into WHEELHOUSE first (including pip); build verifies
closure offline with pip's resolver. Publish OUTPUT at seed_path() only after
root ownership and 0555 directories/0444 files are applied by the image builder.
Consume with: scanner_seed.py install TOOL JOB_VENV. Pins come from PIN_* env.
Probe with the fresh job venv Python: scanner_seed.py probe TOOL. This reads
only seed metadata/bytes and prints cache_mode=offline or cache_mode=fallback.
The reusable workflow embeds these exact bytes, so callers need no hub checkout.
"""

import argparse
import email
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import sysconfig
import tempfile
import zipfile


SEED_ROOT = Path("/opt/ci-seeds/scanners")
PIN_NAMES = ("PIN_PIP", "PIN_SEMGREP", "PIN_ZIZMOR", "PIN_SEMGREP_OTEL")


def pins():
    # Name the unset variables, never their values: a missing pin is a caller
    # mistake the documented build/install CLI can hit, so it must surface as
    # the module's controlled ValueError, not a bare KeyError traceback.
    missing = [name for name in PIN_NAMES if name not in os.environ]
    if missing:
        raise ValueError("Missing scanner pin environment variable(s): " + ", ".join(missing))
    values = {name: os.environ[name] for name in PIN_NAMES}
    if not all(re.fullmatch(r"[0-9][a-zA-Z0-9.]*", v) for v in values.values()):
        raise ValueError("Invalid scanner pins")
    return values


def identity(tool):
    return {"tool": tool, "pins": pins(), "abi": sysconfig.get_config_var("SOABI"),
            "os": platform.system(), "arch": platform.machine()}


def seed_path(wanted):
    return SEED_ROOT / wanted["tool"] / wanted["abi"] / wanted["os"] / wanted["arch"]


def roots(wanted):
    p = wanted["pins"]
    packages = {"pip": p["PIN_PIP"], wanted["tool"]: p["PIN_" + wanted["tool"].upper()]}
    if wanted["tool"] == "semgrep":
        packages.update({"opentelemetry-instrumentation-" + name: p["PIN_SEMGREP_OTEL"]
                         for name in ("requests", "threading")})
    return packages


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_record(path):
    if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
        raise ValueError("Wheelhouse must contain only regular wheel files")
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(names) != len(set(names)) or len(metadata) != 1:
            raise ValueError("Duplicate or missing wheel metadata")
        meta = email.message_from_bytes(wheel.read(metadata[0]))
    if any("@" in dep or "://" in dep for dep in meta.get_all("Requires-Dist", [])):
        raise ValueError("Seed dependencies must not use direct URLs")
    name, version = canonical(meta["Name"] or ""), meta["Version"] or ""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not re.fullmatch(r"[a-zA-Z0-9.+!]+", version):
        raise ValueError("Invalid wheel name/version")
    return {"file": path.name, "name": name, "version": version,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def requirements(records):
    return "".join(f'{r["name"]}=={r["version"]} --hash=sha256:{r["sha256"]}\n' for r in records)


def pip_command(python, *args):
    env = None
    if "--no-index" in args:
        # --no-index alone still permits configured URL find-links/targets.
        # Offline seeds use only their wheel directory and the job venv.
        env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
        env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([str(python), "-I", "-m", "pip", *args], env=env, check=True, timeout=240)


def build(tool, wheelhouse, output):
    wanted = identity(tool)
    records = sorted((wheel_record(p) for p in wheelhouse.iterdir()), key=lambda r: r["name"])
    if not records or len({r["name"] for r in records}) != len(records):
        raise ValueError("Empty or duplicate wheelhouse")
    # Resolve with the image's actual interpreter and no network. Comparing the
    # report to the entire wheelhouse rejects irrelevant/unresolved extra wheels.
    with tempfile.TemporaryDirectory() as temp:
        report = Path(temp) / "report.json"
        pip_command(sys.executable, "install", "--dry-run", "--ignore-installed",
                    "--no-index", "--no-cache-dir", "--only-binary=:all:", "--find-links", str(wheelhouse),
                    "--report", str(report), *[f"{n}=={v}" for n, v in roots(wanted).items()])
        resolved = {canonical(r["metadata"]["name"]): r["metadata"]["version"]
                    for r in json.loads(report.read_text())["install"]}
    if resolved != {r["name"]: r["version"] for r in records}:
        raise ValueError("Wheelhouse is not the complete exact resolved dependency closure")
    output.mkdir(parents=True, exist_ok=False)
    wheels = output / "wheels"
    wheels.mkdir()
    for record in records:
        (wheels / record["file"]).write_bytes((wheelhouse / record["file"]).read_bytes())
    (output / "manifest.json").write_text(json.dumps(
        {"schema_version": 1, "identity": wanted, "wheels": records}, sort_keys=True, indent=2) + "\n")
    (output / "requirements.txt").write_text(requirements(records))


def immutable(path, *, directory=False, ancestor=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != 0 or info.st_mode & (0o022 if ancestor else 0o222):
        raise ValueError(f"Seed must be immutable root-owned regular files/directories: {path}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest field")
        result[key] = value
    return result


def verify_closure(seed, wanted, records):
    # Use ensurepip's trusted parser, never import anything from seed wheels.
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import parse_wheel_filename
    from pip._vendor.packaging.version import Version

    packages = {r["name"]: r for r in records}
    dependencies = {}
    supported = set(sys_tags())
    for record in records:
        name, version, _, tags = parse_wheel_filename(record["file"])
        if canonical(name) != record["name"] or str(version) != record["version"] or not tags & supported:
            raise ValueError("Wheel filename/ABI does not match seed")
        with zipfile.ZipFile(seed / "wheels" / record["file"]) as wheel:
            metadata = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
            meta = email.message_from_bytes(wheel.read(metadata))
        if meta["Requires-Python"]:
            from pip._vendor.packaging.specifiers import SpecifierSet
            if not SpecifierSet(meta["Requires-Python"]).contains(platform.python_version()):
                raise ValueError("Wheel requires a different Python version")
        dependencies[record["name"]] = [Requirement(d) for d in meta.get_all("Requires-Dist", [])]
    pending = [(name, "") for name in roots(wanted)]
    seen = set()
    while pending:
        name, extra = pending.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        for dep in dependencies[name]:
            if dep.marker and not dep.marker.evaluate({"extra": extra}):
                continue
            target = canonical(dep.name)
            if target not in packages or not dep.specifier.contains(Version(packages[target]["version"]), prereleases=True):
                raise ValueError("Seed has an incomplete or incompatible dependency closure")
            pending.extend((target, e) for e in ("", *dep.extras))
    if {name for name, _ in seen} != set(packages):
        raise ValueError("Seed contains wheels outside its dependency closure")


def verify(seed, wanted):
    if not os.path.lexists(seed):
        return None
    for parent in reversed(seed.parents):
        immutable(parent, directory=True, ancestor=True)
    immutable(seed, directory=True)
    manifest_path = seed / "manifest.json"
    # A partially provisioned seed (the directory exists, the manifest does
    # not) is a controlled failure, not a raw FileNotFoundError from lstat.
    # probe/install must exit non-zero through ValueError so the workflow's
    # probe step can decide cache_mode without a traceback.
    if not os.path.lexists(manifest_path):
        raise ValueError("Seed directory is missing manifest.json")
    immutable(manifest_path)
    manifest = json.loads(manifest_path.read_text(), object_pairs_hook=unique_object)
    if set(manifest) != {"schema_version", "identity", "wheels"} or manifest["schema_version"] != 1:
        raise ValueError("Invalid seed schema")
    if manifest["identity"] != wanted:
        return None
    if wanted["os"] != "Linux":
        return None
    if {p.name for p in seed.iterdir()} != {"manifest.json", "requirements.txt", "wheels"}:
        raise ValueError("Unexpected or missing seed files")
    immutable(seed / "wheels", directory=True)
    immutable(seed / "requirements.txt")
    records = manifest["wheels"]
    if not isinstance(records, list) or not records:
        raise ValueError("Empty wheel manifest")
    names, files = set(), set()
    for record in records:
        if set(record) != {"file", "name", "version", "sha256"}:
            raise ValueError("Invalid wheel record")
        filename = record["file"]
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".whl"):
            raise ValueError("Invalid wheel path")
        if record["name"] in names or filename in files:
            raise ValueError("Duplicate wheel record")
        names.add(record["name"])
        files.add(filename)
        path = seed / "wheels" / filename
        immutable(path)
        if wheel_record(path) != record:
            raise ValueError("Wheel bytes/metadata do not match manifest")
    if {p.name for p in (seed / "wheels").iterdir()} != files:
        raise ValueError("Unexpected or missing wheels")
    versions = {r["name"]: r["version"] for r in records}
    if any(versions.get(n) != v for n, v in roots(wanted).items()):
        raise ValueError("Missing or incorrect pinned wheel")
    if (seed / "requirements.txt").read_text() != requirements(records):
        raise ValueError("Requirements do not match complete wheel manifest")
    verify_closure(seed, wanted, records)
    return records


def probe(tool):
    wanted = identity(tool)
    records = verify(seed_path(wanted), wanted)
    print("cache_mode=" + ("fallback" if records is None else "offline"))


def install(tool, venv):
    wanted = identity(tool)
    seed = seed_path(wanted)
    records = verify(seed, wanted)
    # This interpreter was created with venv --clear by the workflow. Never use
    # a seed executable, ambient scanner, user site, or caller Python modules.
    python = venv / "bin/python"
    subprocess.run([str(python), "-I", "-c",
                    f"import sys; assert sys.prefix == {str(venv.absolute())!r} "
                    "and sys.prefix != sys.base_prefix, 'Not a job venv'"], check=True, timeout=30)
    if records is None:
        bounded = ("--disable-pip-version-check", "--no-input", "--progress-bar", "off",
                   "--timeout", "30", "--retries", "2", "--only-binary=:all:")
        packages = roots(wanted)
        pip_command(python, "install", *bounded, f'pip=={packages.pop("pip")}')
        pip_command(python, "install", *bounded, *[f"{n}=={v}" for n, v in packages.items()])
    else:
        # Drop ensurepip's optional setuptools so the final inventory is exact.
        pip_command(python, "uninstall", "--yes", "setuptools")
        pip_command(python, "install", "--disable-pip-version-check", "--no-input",
                    "--no-index", "--no-cache-dir", "--only-binary=:all:", "--require-hashes", "--force-reinstall",
                    "--find-links", str(seed / "wheels"), "-r", str(seed / "requirements.txt"))
    pip_command(python, "check")
    expected = roots(wanted) if records is None else {r["name"]: r["version"] for r in records}
    check = (
        "import importlib.metadata as m, json, re, sys; "
        f"assert sys.prefix == {str(venv.absolute())!r} and sys.prefix != sys.base_prefix, 'Not a job venv'; "
        "distributions=list(m.distributions()); "
        "actual={re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in distributions}; "
        "assert len(actual)==len(distributions), 'Duplicate installed distributions'; "
        "expected=json.loads(sys.argv[1]); "
        "assert all(actual.get(n)==v for n,v in expected.items()), (expected,actual); "
        + ("assert actual == expected, (expected,actual)" if records is not None else "")
    )
    subprocess.run([str(python), "-I", "-c", check, json.dumps(expected)], check=True, timeout=30)
    executable = venv / "bin" / tool
    if executable.is_symlink() or not executable.is_file():
        raise ValueError("Missing or symlinked job-local scanner")
    scanner_env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    output = subprocess.check_output([str(executable), "--version"], env=scanner_env,
                                     text=True, timeout=30).strip()
    version = wanted["pins"]["PIN_" + tool.upper()]
    if output not in {version, f"{tool} {version}", f"{tool} v{version}"}:
        raise ValueError("Scanner executable version mismatch")
    print(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("build", "install", "probe"))
    parser.add_argument("tool", choices=("semgrep", "zizmor"))
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    args = parser.parse_args()
    if args.mode == "probe":
        probe(args.tool)
    elif args.path is None:
        parser.error("build/install requires PATH")
    elif args.mode == "build":
        if args.output is None:
            parser.error("build requires OUTPUT")
        build(args.tool, args.path, args.output)
    else:
        install(args.tool, args.path)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
