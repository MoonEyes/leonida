"""Shared helpers for paquet.py and rougevert.py.

Pull request code is untrusted. It only ever runs inside a throwaway Docker container
with no network, no token, no access to the git directory and no access to the runner's
files: each test run gets a fresh copy of the work tree without .git.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid

REPO = "repo"
UPSTREAM = "https://github.com/ProjectLeonida/mandy.git"
IMAGE = "python:3.13-bookworm"
EXCLUDED = {"tests/test_bcpack_native.py", "tests/test_cmake_xma2_prebuilt.py",
            "tests/test_world_grid_atlas_native.py"}
RISKY = re.compile(r"cargo\b|Get-NVIDIA|get-nvidia-sdks|build_llvm_mc|FetchContent")
FAIL_LINE = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)", re.M)
PYTEST_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
DEADLINE = time.time() + 125 * 60  # leave room before the 150 min job limit
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


class Budget(Exception):
    pass


def run(cmd, cwd=None, check=True, timeout=None, env=None):
    """Run a command in its own process group; kill the whole group on timeout."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", env=env,
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        raise
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} -> {p.returncode}\n{err[-1500:]}")
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def git(*args, check=True):
    return run([*GIT, *args], cwd=REPO, check=check, timeout=600)


def clone():
    run([*GIT, "clone", "-q", "--filter=blob:none", UPSTREAM, REPO], timeout=900)
    return git("rev-parse", "origin/main").stdout.strip()


def switch(ref, branch=None):
    """Force the work tree onto ref and remove everything untracked."""
    if branch:
        git("checkout", "-q", "-f", "-B", branch, ref)
    else:
        git("checkout", "-q", "-f", "--detach", ref)
    git("clean", "-q", "-fdx")


def load_states(path):
    with open(path, encoding="utf-8") as f:
        return {int(k): v for k, v in json.load(f).items()}


def fetch_pr(n, states):
    """Returns (head, None) or (None, reason)."""
    st = states.get(n)
    if not st:
        return None, "not found"
    if st["state"] != "OPEN":
        return None, st["state"].lower()
    if st["isDraft"]:
        return None, "draft"
    if git("fetch", "-q", "origin", f"pull/{n}/head:pr{n}", check=False).returncode != 0:
        return None, "fetch failed"
    head = git("rev-parse", f"pr{n}").stdout.strip()
    if head != st["headRefOid"]:
        return None, "head moved during the run"
    return head, None


def test_command(path, src):
    # Not "python -I": it would drop PYTHONPATH (the test dependencies in /deps) and the
    # script's own folder (tests import sibling helpers). Isolation comes from the container.
    if path.startswith("tests/semantic/"):
        return ["python", "-m", "unittest", "discover", "-s", "tests/semantic", "-p", os.path.basename(path)]
    has_unittest = "import unittest" in src or "from unittest" in src
    if has_unittest and "__main__" in src:
        return ["python", path]
    if has_unittest:
        # tests/ is on PYTHONPATH (see in_container), so the module name is the file name.
        return ["python", "-m", "unittest", "-v", os.path.splitext(os.path.basename(path))[0]]
    return ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", path]


def in_container(cmd, timeout):
    """Run cmd on a fresh copy of the work tree (no .git) in an isolated container."""
    if time.time() + timeout > DEADLINE:
        raise Budget()
    tree = os.path.abspath(f"tree-{uuid.uuid4().hex[:8]}")
    shutil.copytree(REPO, tree, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    name = f"t{uuid.uuid4().hex[:12]}"
    docker = ["docker", "run", "--rm", "--name", name, "--network", "none",
              "--user", f"{os.getuid()}:{os.getgid()}",
              "--memory", "6g", "--pids-limit", "512", "--cap-drop", "ALL",
              "--security-opt", "no-new-privileges", "-v", f"{tree}:/src", "-w", "/src",
              "-v", f"{os.path.abspath('deps')}:/deps:ro", "-e", "PYTHONPATH=/deps:/src/tests",
              "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", IMAGE, *cmd]
    try:
        p = run(docker, check=False, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        return None, "<timeout>"
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def failing_ids(rc, out):
    """None when the run passed, else the failing case ids (or a marker when none parse)."""
    if rc == 0:
        return None
    if rc is None:
        return ["<timeout>"]
    ids = sorted(set(f"{m[0]} ({m[1]})" for m in FAIL_LINE.findall(out)) | set(PYTEST_LINE.findall(out)))
    return ids or [f"<rc {rc}, no case id>"]


def is_assertion_red(out):
    """True when the output shows an assertion failure and no import error."""
    if re.search(r"ModuleNotFoundError|ImportError|cannot import name", out):
        return False
    if re.search(r"^ERROR[: ]", out, re.M):
        return False
    return bool(re.search(r"AssertionError|^FAIL: |^FAILED \S+ - (?:AssertionError|assert)", out, re.M))


def md(text):
    """Make PR-provided text safe inside a Markdown table cell."""
    s = str(text)
    for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("|", "\\|"), ("`", "'"),
                 ("[", "\\["), ("]", "\\]"), ("!", "\\!"), ("\n", " ")):
        s = s.replace(a, b)
    return s[:300]


def write_report(out_dir, name, report, lines):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False, sort_keys=True, default=str)
    with open(os.path.join(out_dir, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write("\n\n".join(lines) + "\n")
    print("\n".join(lines))
