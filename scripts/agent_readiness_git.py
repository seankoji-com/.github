"""Selected-history Git operations for the readiness CI ratchet.

Keep credentials in subprocess environment only. Fetch complete commit history
for the checked-out commit and base branch, without historical file contents,
other branches, tags, submodules or LFS objects.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def prepare_ci_git(root: Path, base_ref: str):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    token = env.pop("AGENT_READINESS_GIT_TOKEN", "")
    config = {"core.hooksPath": "/dev/null"}
    if token:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        parsed = urlsplit(server)
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository)):
            raise ValueError("invalid GitHub server or repository for readiness fetch")
        origin = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                                check=True, timeout=10, capture_output=True, text=True).stdout.strip()
        expected = f"{server}/{repository}"
        if origin not in (expected, expected + ".git"):
            raise ValueError("readiness origin does not match the GitHub event repository")
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        config.update({"credential.helper": "", "http.followRedirects": "false",
                       f"http.{server}/.extraheader": f"AUTHORIZATION: basic {auth}"})
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    for index, (key, value) in enumerate(config.items(), count):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + len(config))

    def git(*args: str, timeout: int = 60):
        return subprocess.run(["git", "-C", str(root), *args], env=env,
                              check=True, timeout=timeout, capture_output=True, text=True)

    git("check-ref-format", f"refs/heads/{base_ref}")
    head = git("rev-parse", "HEAD").stdout.strip()
    shallow = git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    # The checked-out SHA can be a synthetic PR merge whose other parent belongs
    # to a fork. Fetch that exact commit from the base repository's origin.
    git("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--filter=blob:none",
        *(["--unshallow"] if shallow else []), "origin",
        f"+{head}:refs/agent-readiness/head",
        f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}", timeout=180)
    if git("rev-parse", "--is-shallow-repository").stdout.strip() != "false":
        raise ValueError("readiness history remains shallow; exact merge-base unavailable")
    return git
