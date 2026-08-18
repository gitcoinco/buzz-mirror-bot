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
    # Two allowlisted announcing keys, comma-separated (with the whitespace
    # a human edits into an env file).
    BUZZ_REPO_OWNER=" %s , %s " % ("1" * 64, "2" * 64),
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
OWNER1, OWNER2 = "1" * 64, "2" * 64
sync.buzz_url = lambda buzz_owner, repo_id: BUZZ
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


def has_main(repo):
    # Not tip() == "": rev-parse echoes an unresolvable arg to stdout.
    return subprocess.run(["git", "-C", repo, "show-ref", "--verify", "-q",
                           "refs/heads/main"], capture_output=True).returncode == 0


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        globals()["FAILED"] = True


FAILED = False

# 1. in sync -> no push, no message
work, base = scenario("in sync")
sync.reconcile(OWNER1, "r", "o/r", "tok", {})
check("github unchanged", tip(GITHUB) == base)
check("silent", POSTS == [])

# 2. buzz ahead -> fast-forward github
work, base = scenario("buzz ahead")
new = commit(work, "buzz work")
sh("git", "push", "-q", BUZZ, "main:main", cwd=work)
sync.reconcile(OWNER1, "r", "o/r", "tok", {})
check("github fast-forwarded to buzz tip", tip(GITHUB) == new)
check("silent on the happy path", POSTS == [])

# 3. github ahead -> adopt it onto buzz main. A strict ancestor is an update,
#    not a conflict, so this is the same non-force push as the other direction.
work, base = scenario("github ahead")
new = commit(work, "github work")
sh("git", "push", "-q", GITHUB, "main:main", cwd=work)
state = {}
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("buzz main fast-forwarded to github tip", tip(BUZZ) == new)
check("not halted", not state.get("r", {}).get("halted"))
check("said which way it went", any("from GitHub" in p for p in POSTS))
branches = subprocess.run(["git", "-C", BUZZ, "branch", "--list", "mirror/*"],
                          capture_output=True, text=True).stdout
check("no proposal branch was needed", branches.strip() == "")

# 3b. adopting converges the tips, so the next tick is silent - the notice is
#     one per adopted push, not one per tick.
POSTS.clear()
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
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
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
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
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("sticky: no repeat message on the next tick", POSTS == [])

# 4. diverged -> halt, nothing pushed anywhere
work, base = scenario("diverged")
b = commit(work, "buzz side")
sh("git", "push", "-q", BUZZ, "main:main", cwd=work)
sh("git", "reset", "-q", "--hard", base, cwd=work)
g = commit(work, "github side")
sh("git", "push", "-qf", GITHUB, "main:main", cwd=work)
state = {}
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("buzz main untouched", tip(BUZZ) == b)
check("github main untouched", tip(GITHUB) == g)
check("halted as diverged", state["r"]["halted"] == "diverged")
check("said it needs a human", any("human" in p for p in POSTS))

# 5. recovery clears the halt and says so
POSTS.clear()
sh("git", "push", "-qf", GITHUB, f"{b}:main", cwd=work)
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("halt cleared", not state["r"].get("halted"))
check("announced recovery", any("recovered" in p for p in POSTS))

# 6. empty buzz repo, github has history -> bootstrap: create buzz main.
#    The onboarding shape: a repo announced on Buzz before anything was pushed.
work, base = scenario("empty buzz bootstraps from github")
sh("git", "-C", BUZZ, "update-ref", "-d", "refs/heads/main")
new = commit(work, "github work")
sh("git", "push", "-q", GITHUB, "main:main", cwd=work)
state = {}
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("buzz main created at github tip", tip(BUZZ) == new)
check("not halted", not state.get("r", {}).get("halted"))
check("announced the bootstrap", any("empty Buzz repo" in p for p in POSTS))

# 6b. converged now, so the next tick is silent
POSTS.clear()
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("silent once the tips match", POSTS == [])

# 6c. a stray branch on the empty buzz side (a proposal branch from an earlier
#     tick, say) does not change the case: main is still absent.
work, base = scenario("empty buzz with a stray branch")
sh("git", "-C", BUZZ, "update-ref", "-d", "refs/heads/main")
sh("git", "push", "-q", BUZZ, "main:refs/heads/mirror/leftover", cwd=work)
sync.reconcile(OWNER1, "r", "o/r", "tok", {})
check("buzz main still bootstrapped", tip(BUZZ) == base)

# 6d. bootstrap refused by the relay -> same proposal fallback as 3c
work, base = scenario("bootstrap refused, propose instead")
sh("git", "-C", BUZZ, "update-ref", "-d", "refs/heads/main")
with open(os.path.join(BUZZ, "hooks", "pre-receive"), "w") as f:
    f.write('#!/bin/sh\nwhile read o n ref; do\n'
            '  [ "$ref" = refs/heads/main ] && { echo "requires Owner role" >&2; exit 1; }\n'
            'done\nexit 0\n')
os.chmod(os.path.join(BUZZ, "hooks", "pre-receive"), 0o755)
state = {}
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("buzz main NOT created when the push is refused", not has_main(BUZZ))
branches = subprocess.run(["git", "-C", BUZZ, "branch", "--list", "mirror/*"],
                          capture_output=True, text=True).stdout
check("fell back to a proposal branch", f"github-ahead-{base[:7]}" in branches)
check("halted as github-ahead", state["r"]["halted"] == "github-ahead")

# 7. buzz has history, github repo is truly empty -> seed it
work, base = scenario("empty github seeded from buzz")
sh("git", "-C", GITHUB, "update-ref", "-d", "refs/heads/main")
state = {}
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("github main created at buzz tip", tip(GITHUB) == base)
check("not halted", not state.get("r", {}).get("halted"))
check("silent, like the steady-state direction", POSTS == [])

# 8. github has branches but no main -> halt, do not guess
work, base = scenario("github default branch is not main")
sh("git", "push", "-q", GITHUB, "main:refs/heads/master", cwd=work)
sh("git", "-C", GITHUB, "update-ref", "-d", "refs/heads/main")
state = {}
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("no main was invented on github", not has_main(GITHUB))
check("halted as github-no-main", state["r"]["halted"] == "github-no-main")
check("said what to do about it", any("Rename the default branch" in p for p in POSTS))
POSTS.clear()
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("sticky: no repeat message on the next tick", POSTS == [])

# 9. both sides empty -> nothing to do, and not an error
work, base = scenario("both sides empty")
sh("git", "-C", BUZZ, "update-ref", "-d", "refs/heads/main")
sh("git", "-C", GITHUB, "update-ref", "-d", "refs/heads/main")
state = {}
sync.reconcile(OWNER1, "r", "o/r", "tok", state)
check("no refs created anywhere", not has_main(BUZZ) and not has_main(GITHUB))
check("not halted", not state.get("r", {}).get("halted"))
check("silent", POSTS == [])

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
BUZZ_REPOS_BY_OWNER = {
    OWNER1: [
        {"tags": [["d", "regenos-dev"], ["web", "https://github.com/irlfund/regenOS"]]},
        {"tags": [["d", "local-almanac-mirror"],
                  ["web", "https://github.com/irlfund/local-almanac.git"]]},
        {"tags": [["d", "not-on-github"]]},                                  # no web tag
        {"tags": [["d", "elsewhere"], ["web", "https://gitlab.com/x/y"]]},   # not GitHub
        {"tags": [["d", "buzz-only"], ["web", "https://github.com/irlfund/never-granted"]]},
    ],
    # A second announcing key (the repobot shape): its repos join the same set.
    OWNER2: [
        {"tags": [["d", "agentic-engineering-infra"],
                  ["web", "https://github.com/irlfund/agentic-engineering-infra/"]]},
    ],
}
sync.buzz_cli = lambda *a: json.dumps(BUZZ_REPOS_BY_OWNER.get(a[-1], []))

bz, bz_ok = sync.discover_buzz()
check("a clean buzz discovery reports ok", bz_ok)
check("pairing is read off the announcement, not guessed from the name",
      bz["regenos-dev"] == (OWNER1, "irlfund/regenOS"))
check("a trailing .git does not change the pairing",
      bz["local-almanac-mirror"] == (OWNER1, "irlfund/local-almanac"))
check("a trailing slash does not change the pairing, and a second allowlisted "
      "owner's repos join the set carrying that owner",
      bz["agentic-engineering-infra"] == (OWNER2, "irlfund/agentic-engineering-infra"))
check("a repo with no web tag is not a candidate", "not-on-github" not in bz)
check("a non-GitHub web tag is not a candidate", "elsewhere" not in bz)

pairs, ok = sync.discover()
got = {r: g for r, _, g, _ in pairs}
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
      all(t == "tok-11" for _, _, _, t in pairs))
check("each pair carries its announcing owner",
      {r: o for r, o, _, _ in pairs}["agentic-engineering-infra"] == OWNER2)

# The deployed shape gets an installation token from the issuer instead of the
# PEM, and that token must be installation-WIDE: narrowing it to a repo list
# would move enrolment out of the App grant and into the issuer's flags.
API_CALLS.clear()
sync.INSTALL_TOKEN = "tok-11"
gh = sync.discover_github()
check("a supplied token is used as-is", "/app/installations" not in " ".join(API_CALLS))
check("no App JWT is minted when a token is supplied",
      not any("access_tokens" in c for c in API_CALLS))
check("the token's own grants are the mirror set",
      sorted(v[0] for v in gh.values()) ==
      ["irlfund/agentic-engineering-infra", "irlfund/local-almanac", "irlfund/regenOS"])
sync.INSTALL_TOKEN = ""

# Two buzz repos claiming one GitHub repo would take turns fast-forwarding the
# same main from unrelated histories. There is no safe guess about which wins,
# so both are dropped and the run fails rather than thrashing quietly.
CONTESTED_BY_OWNER = {
    OWNER1: BUZZ_REPOS_BY_OWNER[OWNER1],
    # The dupe claim arriving from a DIFFERENT allowlisted owner must be
    # caught the same way as one from the same owner.
    OWNER2: BUZZ_REPOS_BY_OWNER[OWNER2] + [
        {"tags": [["d", "regenos-dupe"], ["web", "https://github.com/irlfund/regenOS"]]},
    ],
}
sync.buzz_cli = lambda *a: json.dumps(CONTESTED_BY_OWNER.get(a[-1], []))
pairs, ok = sync.discover()
names = [r for r, _, _, _ in pairs]
check("a contested GitHub repo fails the run", not ok)
check("both claimants are dropped, not just the loser",
      "regenos-dev" not in names and "regenos-dupe" not in names)
check("uncontested repos still mirror", "local-almanac-mirror" in names)

# The relay accepts a second announcement of a reserved repo id (the
# reservation fails post-ingest; the event stores and lists but the repo
# behind it 404s forever). During migration repobot could announce an id
# planbot holds; the pair must drop AND the run must fail loudly, or a repo
# that was mirroring fine stops mirroring with exit 0.
DUP_ID_BY_OWNER = {
    OWNER1: BUZZ_REPOS_BY_OWNER[OWNER1],
    OWNER2: BUZZ_REPOS_BY_OWNER[OWNER2] + [
        {"tags": [["d", "regenos-dev"], ["web", "https://github.com/irlfund/regenOS"]]},
    ],
}
sync.buzz_cli = lambda *a: json.dumps(DUP_ID_BY_OWNER.get(a[-1], []))
pairs, ok = sync.discover()
names = [r for r, _, _, _ in pairs]
check("a repo id announced by two allowlisted owners fails the run", not ok)
check("the duplicated id is dropped entirely", "regenos-dev" not in names)
check("other repos still mirror around a duplicated id",
      "local-almanac-mirror" in names and "agentic-engineering-infra" in names)
sync.buzz_cli = lambda *a: json.dumps(BUZZ_REPOS_BY_OWNER.get(a[-1], []))


# ---------------------------------------------------------------------------
# transient retry: a blip that clears in seconds must not page anyone
# ---------------------------------------------------------------------------

print("\n--- transient retry")

sync.GIT_RETRY_DELAY = 0
real_run = sync.run
RUN_CALLS = []


def flaky_then_ok(args, **kw):
    RUN_CALLS.append(args)
    if len(RUN_CALLS) < 3:
        raise RuntimeError(
            "git failed (128): remote: Internal Server Error\nfatal: unable to "
            "access 'https://github.com/o/r.git/': The requested URL returned error: 500")
    return "ok"


sync.run = flaky_then_ok
check("a 5xx is retried until it clears", sync.git("/tmp", "fetch") == "ok")
check("it took all three attempts", len(RUN_CALLS) == 3)

RUN_CALLS.clear()


def denied(args, **kw):
    RUN_CALLS.append(args)
    raise RuntimeError("git failed (128): remote: requires Owner role")


sync.run = denied
try:
    sync.git("/tmp", "push")
    raised = False
except RuntimeError:
    raised = True
check("an auth denial is not retried", raised and len(RUN_CALLS) == 1)

RUN_CALLS.clear()


def always_502(args, **kw):
    RUN_CALLS.append(args)
    raise RuntimeError("fatal: unable to access 'https://github.com/o/r.git/': "
                       "The requested URL returned error: 502")


sync.run = always_502
try:
    sync.git("/tmp", "fetch")
    raised = False
except RuntimeError:
    raised = True
check("a persistent 5xx still raises after the last attempt",
      raised and len(RUN_CALLS) == sync.GIT_TRIES)

sync.run = real_run


# ---------------------------------------------------------------------------
# --once exit status: for a scheduled task, the exit code IS the alert
# ---------------------------------------------------------------------------

print("\n--- --once exit status")

sync.reconcile = lambda buzz_owner, repo_id, gh_repo, tok, st: None
sync.load_state = lambda: {}
sync.save_state = lambda s: None
touched = []
sync.touch_last_success = lambda: touched.append(1)
sys.argv = ["sync.py", "--once"]
check("clean run exits 0", sync.main() == 0)
check("clean run recorded success", len(touched) == 1)


def boom(buzz_owner, repo_id, gh_repo, tok, st):
    raise RuntimeError("api.github.com exploded")


sync.reconcile = boom
POSTS.clear(); touched.clear()
check("failed run exits non-zero", sync.main() == 1)
check("failed run did NOT record success", touched == [])
check("failure is attributed to github", any("github-auth-failed" in p for p in POSTS))


def boom_500(buzz_owner, repo_id, gh_repo, tok, st):
    raise RuntimeError("fatal: unable to access 'https://github.com/o/r.git/': "
                       "The requested URL returned error: 500")


# A 5xx that survives the in-tick retries still halts, but under an honest
# label: `github-unavailable`, not `github-auth-failed`. Halts are sticky by
# reason, so the wrong label would also swallow a real auth failure that
# arrived while the 5xx halt was live.
sync.reconcile = boom_500
POSTS.clear(); touched.clear()
check("a persistent 5xx still fails the run", sync.main() == 1)
check("but is labelled unavailable, not an auth failure",
      any("github-unavailable" in p for p in POSTS)
      and not any("auth-failed" in p for p in POSTS))

# Discovering nothing must fail loudly: an empty mirror set reads exactly like
# "everything is in sync", and it is the shape a mis-click produces.
sync.reconcile = lambda buzz_owner, repo_id, gh_repo, tok, st: None
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
