#!/usr/bin/env python3
"""Exercise the reconcile decision tree against real git repos.

The expensive bug in this daemon is pushing the wrong direction, so the four
ancestry cases are tested against actual repositories rather than mocks: two
bare repos standing in for Buzz and GitHub, driven through `reconcile()` with
only the network-facing edges (PR creation, channel posts) stubbed.

Run: python3 mirror/test_reconcile.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

# PyJWT is a container dependency; the decision tree does not touch it.
sys.modules.setdefault("jwt", types.ModuleType("jwt"))

TMP = tempfile.mkdtemp(prefix="mirror-test-")
os.environ.update(
    MIRROR_STATE_DIR=os.path.join(TMP, "state"),
    MIRROR_ALERT_CHANNEL="test-channel",
    GITHUB_APP_ID="1",
    MIRROR_REPOS="{}",
    BUZZ_REPO_OWNER="deadbeef",
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daemon  # noqa: E402

POSTS = []
daemon.post = lambda text: POSTS.append(text)
daemon.open_pr = lambda *a, **k: "buzz://pr/test"

BUZZ = os.path.join(TMP, "buzz.git")
GITHUB = os.path.join(TMP, "github.git")

# Route both "remotes" at local bare repos. The daemon's own git invocation,
# fetch/push logic and ancestry checks are all exercised for real.
daemon.buzz_url = lambda repo_id: BUZZ
daemon.github_url = lambda gh_repo, token: GITHUB


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def commit(work, msg):
    with open(os.path.join(work, "f"), "a") as f:
        f.write(msg + "\n")
    sh("git", "add", "f", cwd=work)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg, cwd=work)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True).stdout.strip()


def scenario(name):
    """Fresh Buzz + GitHub repos sharing one base commit."""
    for p in (BUZZ, GITHUB, os.path.join(TMP, "state")):
        shutil.rmtree(p, ignore_errors=True)
    sh("git", "init", "--bare", "-q", BUZZ)
    sh("git", "init", "--bare", "-q", GITHUB)
    work = os.path.join(TMP, "work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    sh("git", "init", "-q", "-b", "main", work)
    base = commit(work, "base")
    sh("git", "push", "-q", BUZZ, "main:main", cwd=work)
    sh("git", "push", "-q", GITHUB, "main:main", cwd=work)
    POSTS.clear()
    print(f"\n--- {name}")
    return work, base


def tip(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "refs/heads/main"],
                          capture_output=True, text=True).stdout.strip()


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        globals()["FAILED"] = True


FAILED = False

# 1. in sync -> no push, no message
work, base = scenario("in sync")
daemon.reconcile("r", "o/r", "tok", {})
check("github unchanged", tip(GITHUB) == base)
check("silent", POSTS == [])

# 2. buzz ahead -> fast-forward github
work, base = scenario("buzz ahead")
new = commit(work, "buzz work")
sh("git", "push", "-q", BUZZ, "main:main", cwd=work)
daemon.reconcile("r", "o/r", "tok", {})
check("github fast-forwarded to buzz tip", tip(GITHUB) == new)
check("silent on the happy path", POSTS == [])

# 3. github ahead -> propose only; buzz main untouched
work, base = scenario("github ahead")
new = commit(work, "github work")
sh("git", "push", "-q", GITHUB, "main:main", cwd=work)
state = {}
daemon.reconcile("r", "o/r", "tok", state)
check("buzz main NOT advanced", tip(BUZZ) == base)
branches = subprocess.run(["git", "-C", BUZZ, "branch", "--list", "mirror/*"],
                          capture_output=True, text=True).stdout
check("proposal branch pushed to buzz", f"github-ahead-{new[:7]}" in branches)
check("halted as github-ahead", state["r"]["halted"] == "github-ahead")
check("posted the merge command", any(f"git push buzz {new}" in p for p in POSTS))
check("named the deploy cost", any("frozen" in p for p in POSTS))

# 3b. sticky: same state again -> no duplicate message
POSTS.clear()
daemon.reconcile("r", "o/r", "tok", state)
check("sticky: no repeat message on the next tick", POSTS == [])

# 4. diverged -> halt, nothing pushed anywhere
work, base = scenario("diverged")
b = commit(work, "buzz side")
sh("git", "push", "-q", BUZZ, "main:main", cwd=work)
sh("git", "reset", "-q", "--hard", base, cwd=work)
g = commit(work, "github side")
sh("git", "push", "-qf", GITHUB, "main:main", cwd=work)
state = {}
daemon.reconcile("r", "o/r", "tok", state)
check("buzz main untouched", tip(BUZZ) == b)
check("github main untouched", tip(GITHUB) == g)
check("halted as diverged", state["r"]["halted"] == "diverged")
check("said it needs a human", any("human" in p for p in POSTS))

# 5. recovery clears the halt and says so
POSTS.clear()
sh("git", "push", "-qf", GITHUB, f"{b}:main", cwd=work)
daemon.reconcile("r", "o/r", "tok", state)
check("halt cleared", not state["r"].get("halted"))
check("announced recovery", any("recovered" in p for p in POSTS))

shutil.rmtree(TMP, ignore_errors=True)
print("\nFAILED" if FAILED else "\nall passed")
sys.exit(1 if FAILED else 0)
