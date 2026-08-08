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
    BUZZ_REPO_OWNER="deadbeef",
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync  # noqa: E402

POSTS = []
sync.post = lambda text: POSTS.append(text)
sync.open_pr = lambda *a, **k: "buzz://pr/test"

BUZZ = os.path.join(TMP, "buzz.git")
GITHUB = os.path.join(TMP, "github.git")

# Route both "remotes" at local bare repos. The daemon's own git invocation,
# fetch/push logic and ancestry checks are all exercised for real.
sync.buzz_url = lambda repo_id: BUZZ
sync.github_url = lambda gh_repo, token: GITHUB


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
sync.reconcile("r", "o/r", "tok", {})
check("github unchanged", tip(GITHUB) == base)
check("silent", POSTS == [])

# 2. buzz ahead -> fast-forward github
work, base = scenario("buzz ahead")
new = commit(work, "buzz work")
sh("git", "push", "-q", BUZZ, "main:main", cwd=work)
sync.reconcile("r", "o/r", "tok", {})
check("github fast-forwarded to buzz tip", tip(GITHUB) == new)
check("silent on the happy path", POSTS == [])

# 3. github ahead -> adopt it onto buzz main. A strict ancestor is an update,
#    not a conflict, so this is the same non-force push as the other direction.
work, base = scenario("github ahead")
new = commit(work, "github work")
sh("git", "push", "-q", GITHUB, "main:main", cwd=work)
state = {}
sync.reconcile("r", "o/r", "tok", state)
check("buzz main fast-forwarded to github tip", tip(BUZZ) == new)
check("not halted", not state.get("r", {}).get("halted"))
check("said which way it went", any("from GitHub" in p for p in POSTS))
branches = subprocess.run(["git", "-C", BUZZ, "branch", "--list", "mirror/*"],
                          capture_output=True, text=True).stdout
check("no proposal branch was needed", branches.strip() == "")

# 3b. adopting converges the tips, so the next tick is silent - the notice is
#     one per adopted push, not one per tick.
POSTS.clear()
sync.reconcile("r", "o/r", "tok", state)
check("nothing more to say once the tips match", POSTS == [])

# 3c. protection is per-repo: where the relay refuses the push to main, the
#     propose-only path still runs. No flag day, and a repo that should never be
#     written from GitHub simply keeps `push:owner`.
work, base = scenario("github ahead, buzz main not writable")
with open(os.path.join(BUZZ, "hooks", "pre-receive"), "w") as f:
    f.write('#!/bin/sh\nwhile read o n ref; do\n'
            '  [ "$ref" = refs/heads/main ] && { echo "requires Owner role" >&2; exit 1; }\n'
            'done\nexit 0\n')
os.chmod(os.path.join(BUZZ, "hooks", "pre-receive"), 0o755)
new = commit(work, "github work")
sh("git", "push", "-q", GITHUB, "main:main", cwd=work)
state = {}
sync.reconcile("r", "o/r", "tok", state)
check("buzz main NOT advanced when the push is refused", tip(BUZZ) == base)
branches = subprocess.run(["git", "-C", BUZZ, "branch", "--list", "mirror/*"],
                          capture_output=True, text=True).stdout
check("fell back to a proposal branch", f"github-ahead-{new[:7]}" in branches)
check("halted as github-ahead", state["r"]["halted"] == "github-ahead")
check("posted the merge command", any(f"git push buzz {new}" in p for p in POSTS))
check("named the deploy cost", any("frozen" in p for p in POSTS))

# 3d. the fallback is sticky on the proposed sha, or a repo that stays ahead
#     would alert on every tick.
POSTS.clear()
sync.reconcile("r", "o/r", "tok", state)
check("sticky: no repeat message on the next tick", POSTS == [])

# 4. diverged -> halt, nothing pushed anywhere
work, base = scenario("diverged")
b = commit(work, "buzz side")
sh("git", "push", "-q", BUZZ, "main:main", cwd=work)
sh("git", "reset", "-q", "--hard", base, cwd=work)
g = commit(work, "github side")
sh("git", "push", "-qf", GITHUB, "main:main", cwd=work)
state = {}
sync.reconcile("r", "o/r", "tok", state)
check("buzz main untouched", tip(BUZZ) == b)
check("github main untouched", tip(GITHUB) == g)
check("halted as diverged", state["r"]["halted"] == "diverged")
check("said it needs a human", any("human" in p for p in POSTS))

# 5. recovery clears the halt and says so
POSTS.clear()
sh("git", "push", "-qf", GITHUB, f"{b}:main", cwd=work)
sync.reconcile("r", "o/r", "tok", state)
check("halt cleared", not state["r"].get("halted"))
check("announced recovery", any("recovered" in p for p in POSTS))

# ---------------------------------------------------------------------------
# discovery
#
# What gets mirrored is derived, not configured, so the derivation is the part
# that can silently mirror the wrong thing - or silently mirror nothing.
#
# An App set to "Any account" also gets one installation PER account, and a
# token minted for one installation is not valid for another, so each repo has
# to come back paired with its own account's token.
# ---------------------------------------------------------------------------

print("\n--- discovery")

API_CALLS = []

GRANTS = {
    11: ("irlfund", ["regenOS", "local-almanac", "agentic-engineering-infra"]),
    22: ("gitcoinco", ["some-org-repo"]),
    33: ("Lucian", []),
    44: ("someone-else", ["their-private-thing"]),  # a stranger's install
}


def fake_api(path, token, method="GET", scheme="Bearer"):
    API_CALLS.append(path)
    base = path.split("?")[0]
    # rsplit, not split: `per_page=100&page=1` contains "page=" twice.
    page = int(path.rsplit("page=", 1)[1]) if "page=" in path else 1
    if base == "/app/installations":
        if page > 1:
            return []
        return [{"id": i, "account": {"login": a}} for i, (a, _) in GRANTS.items()]
    if base == "/installation/repositories":
        if page > 1:
            return {"repositories": []}
        acct, names = GRANTS[int(token.split("-")[1])]
        return {"repositories": [{"name": n, "full_name": f"{acct}/{n}"} for n in names]}
    return {"token": "tok-" + path.split("/")[3]}


sync.app_jwt = lambda: "jwt"
sync.api = fake_api

gh = sync.discover_github()
check("every granted repo is found", len(gh) == 5)
check("repos are keyed case-insensitively", "irlfund/regenos" in gh)
check("the real casing is preserved for the URL",
      gh["irlfund/regenos"][0] == "irlfund/regenOS")
check("each repo carries its own installation's token",
      gh["irlfund/regenos"][1] == "tok-11" and gh["gitcoinco/some-org-repo"][1] == "tok-22")

# The Buzz announcement declares the pairing. Two of the three real repos have
# a buzz repo-id that does NOT match the GitHub name, which is exactly why this
# is read rather than guessed.
BUZZ_REPOS = [
    {"tags": [["d", "regenos-dev"], ["web", "https://github.com/irlfund/regenOS"]]},
    {"tags": [["d", "local-almanac-mirror"],
              ["web", "https://github.com/irlfund/local-almanac.git"]]},
    {"tags": [["d", "agentic-engineering-infra"],
              ["web", "https://github.com/irlfund/agentic-engineering-infra/"]]},
    {"tags": [["d", "not-on-github"]]},                                  # no web tag
    {"tags": [["d", "elsewhere"], ["web", "https://gitlab.com/x/y"]]},   # not GitHub
    {"tags": [["d", "buzz-only"], ["web", "https://github.com/irlfund/never-granted"]]},
]
sync.buzz_cli = lambda *a: json.dumps(BUZZ_REPOS)

bz = sync.discover_buzz()
check("pairing is read off the announcement, not guessed from the name",
      bz["regenos-dev"] == "irlfund/regenOS")
check("a trailing .git does not change the pairing",
      bz["local-almanac-mirror"] == "irlfund/local-almanac")
check("a trailing slash does not change the pairing",
      bz["agentic-engineering-infra"] == "irlfund/agentic-engineering-infra")
check("a repo with no web tag is not a candidate", "not-on-github" not in bz)
check("a non-GitHub web tag is not a candidate", "elsewhere" not in bz)

pairs, ok = sync.discover()
got = {r: g for r, g, _ in pairs}
check("a clean discovery reports ok", ok)
check("mirrors exactly the repos that opted in on both sides",
      got == {"regenos-dev": "irlfund/regenOS",
              "local-almanac-mirror": "irlfund/local-almanac",
              "agentic-engineering-infra": "irlfund/agentic-engineering-infra"})
check("announced but never granted is skipped, not mirrored", "buzz-only" not in got)
check("a stranger's installation cannot inject a repo",
      not any(g.startswith("someone-else/") for g in got.values()))
check("granted but not announced is skipped",
      "some-org-repo" not in " ".join(got.values()))
check("each pair carries its own account's token",
      all(t == "tok-11" for _, _, t in pairs))

sync.ONLY = {"regenos-dev"}
check("MIRROR_ONLY restricts to the named repos",
      [r for r, _, _ in sync.discover()[0]] == ["regenos-dev"])
sync.ONLY = set()

# Two buzz repos claiming one GitHub repo would take turns fast-forwarding the
# same main from unrelated histories. There is no safe guess about which wins,
# so both are dropped and the run fails rather than thrashing quietly.
CONTESTED = BUZZ_REPOS + [
    {"tags": [["d", "regenos-dupe"], ["web", "https://github.com/irlfund/regenOS"]]},
]
sync.buzz_cli = lambda *a: json.dumps(CONTESTED)
pairs, ok = sync.discover()
names = [r for r, _, _ in pairs]
check("a contested GitHub repo fails the run", not ok)
check("both claimants are dropped, not just the loser",
      "regenos-dev" not in names and "regenos-dupe" not in names)
check("uncontested repos still mirror", "local-almanac-mirror" in names)
sync.buzz_cli = lambda *a: json.dumps(BUZZ_REPOS)


# ---------------------------------------------------------------------------
# --once exit status: for a scheduled task, the exit code IS the alert
# ---------------------------------------------------------------------------

print("\n--- --once exit status")

sync.reconcile = lambda repo_id, gh_repo, tok, st: None
sync.load_state = lambda: {}
sync.save_state = lambda s: None
touched = []
sync.touch_last_success = lambda: touched.append(1)
sys.argv = ["sync.py", "--once"]
check("clean run exits 0", sync.main() == 0)
check("clean run recorded success", len(touched) == 1)


def boom(repo_id, gh_repo, tok, st):
    raise RuntimeError("api.github.com exploded")


sync.reconcile = boom
POSTS.clear(); touched.clear()
check("failed run exits non-zero", sync.main() == 1)
check("failed run did NOT record success", touched == [])
check("failure is attributed to github", any("github-auth-failed" in p for p in POSTS))

# Discovering nothing must fail loudly: an empty mirror set reads exactly like
# "everything is in sync", and it is the shape a mis-click produces.
sync.reconcile = lambda repo_id, gh_repo, tok, st: None
sync.discover = lambda: ([], True)
touched.clear()
check("discovering no repos exits non-zero", sync.main() == 1)
check("discovering no repos did NOT record success", touched == [])

# Concurrency: Coolify does not dedupe scheduled runs, so an overlapping exec
# must be a no-op rather than two runs racing on the same bare clones.
sync.discover = lambda: ([("r", "o/r", "tok")], True)
held = sync.hold_lock()
check("a second run cannot take the lock", sync.hold_lock() is None)
touched.clear()
check("an overlapping run exits 0, not as a failure", sync.main() == 0)
check("an overlapping run did no work", touched == [])
held.close()
check("the lock is released when the holder exits", sync.hold_lock() is not None)

shutil.rmtree(TMP, ignore_errors=True)
print("\nFAILED" if FAILED else "\nall passed")
sys.exit(1 if FAILED else 0)
