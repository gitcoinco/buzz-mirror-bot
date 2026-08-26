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
# The discovery tests below replace buzz_cli and api with stubs, for the rest of
# the file. Keep the real ones: the retry tests need implementations that
# actually reach run()/urlopen, or they pass against a stub having exercised
# nothing. This bit me once already.
REAL_BUZZ_CLI = sync.buzz_cli
REAL_API = sync.api

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

# --- more than one installation ---------------------------------------------
#
# A token is valid for exactly one installation, so an App installed on several
# accounts needs several tokens. This is what lets the mirror carry a repo
# outside the account that holds everything else - concretely, its own GitHub
# half under gitcoinco while the mirrored repos are granted under irlfund.

check("GITHUB_OWNERS unset is not the same as empty", sync.read_owner_tokens({}) == [])
check("an account name becomes an env var name",
      sync.token_var("gitcoinco") == "GITHUB_INSTALLATION_TOKEN_GITCOINCO")
# GitHub account names allow hyphens; env var names do not. aei's launcher
# derives the same name from the same list, so this translation is a contract
# between the two repos rather than a local detail.
check("a hyphen in the account name becomes an underscore",
      sync.token_var("Some-Org") == "GITHUB_INSTALLATION_TOKEN_SOME_ORG")

ENV_2 = {"GITHUB_OWNERS": "irlfund,gitcoinco",
         "GITHUB_INSTALLATION_TOKEN_IRLFUND": "tok-11",
         "GITHUB_INSTALLATION_TOKEN_GITCOINCO": "tok-22"}
check("commas separate the list",
      sync.read_owner_tokens(ENV_2) == [("irlfund", "tok-11"), ("gitcoinco", "tok-22")])
# The env file this comes from is sourced as bash, where an unquoted space is a
# syntax error. Comma is the form to write there; whitespace is accepted so the
# quoted spelling is not a trap either.
check("whitespace separates the list too, and order is preserved",
      sync.read_owner_tokens(dict(ENV_2, GITHUB_OWNERS=" gitcoinco   irlfund ")) ==
      [("gitcoinco", "tok-22"), ("irlfund", "tok-11")])
check("a repeated account is read once",
      sync.read_owner_tokens(dict(ENV_2, GITHUB_OWNERS="irlfund, irlfund ,gitcoinco")) ==
      [("irlfund", "tok-11"), ("gitcoinco", "tok-22")])

# Fatal, not skipped. Skipping would drop every repo in that account and log a
# clean run - the silent stop this daemon exists to prevent.
try:
    sync.read_owner_tokens({"GITHUB_OWNERS": "irlfund,gitcoinco",
                            "GITHUB_INSTALLATION_TOKEN_IRLFUND": "tok-11"})
    check("a named account with no token refuses to start", False)
except SystemExit as e:
    check("a named account with no token refuses to start", True)
    check("and the message names the account and the variable",
          "gitcoinco" in str(e) and "GITHUB_INSTALLATION_TOKEN_GITCOINCO" in str(e))

# token_var() exists twice: here, and in bash in aei's launcher. `tr` maps bytes
# and str.upper() maps Unicode, so they agree on ASCII letters/digits/hyphen and
# only there - "ß".upper() is "SS" to python and two underscores to tr. Both
# sides refuse anything outside that set, so the contract is exact rather than
# nearly exact, and a bad entry is named as a bad entry instead of surfacing as
# a token that went missing.
for junk in ("gitcoinß", "org.name", "o/rg", "org_name"):
    try:
        sync.read_owner_tokens({"GITHUB_OWNERS": junk})
        check(f"an account name outside [A-Za-z0-9-] is refused: {junk!r}", False)
    except SystemExit as e:
        check(f"an account name outside [A-Za-z0-9-] is refused: {junk!r}",
              "not a GitHub account name" in str(e))
check("a hyphen is still allowed",
      sync.read_owner_tokens({"GITHUB_OWNERS": "my-org",
                              "GITHUB_INSTALLATION_TOKEN_MY_ORG": "t"}) == [("my-org", "t")])

API_CALLS.clear()
sync.INSTALL_TOKENS = sync.read_owner_tokens(ENV_2)
gh = sync.discover_github()
check("the mirror set is the union of both installations' grants",
      sorted(v[0] for v in gh.values()) ==
      ["gitcoinco/some-org-repo", "irlfund/agentic-engineering-infra",
       "irlfund/local-almanac", "irlfund/regenOS"])
check("each repo carries the token for its own account",
      gh["irlfund/regenos"][1] == "tok-11" and gh["gitcoinco/some-org-repo"][1] == "tok-22")
check("supplied tokens are still used as-is, with no App JWT",
      "/app/installations" not in " ".join(API_CALLS)
      and not any("access_tokens" in c for c in API_CALLS))
# A stranger installing an "Any account" App must gain nothing. The PEM path
# walks every installation; naming the accounts is what bounds this one.
check("an installation nobody named is never read",
      not any(v[0].startswith("someone-else/") for v in gh.values()))

# The point of the whole change: a repo in the second account now mirrors.
sync.buzz_cli = lambda *a: json.dumps(dict(
    BUZZ_REPOS_BY_OWNER,
    **{OWNER2: BUZZ_REPOS_BY_OWNER[OWNER2] + [
        {"tags": [["d", "mirror-bot"],
                  ["web", "https://github.com/gitcoinco/some-org-repo"]]}]},
).get(a[-1], []))
pairs, ok = sync.discover()
by_id = {r: (g, t) for r, _, g, t in pairs}
check("a repo in the second account mirrors, carrying that account's token",
      by_id.get("mirror-bot") == ("gitcoinco/some-org-repo", "tok-22"))
check("adding a second account does not disturb the first",
      by_id["regenos-dev"] == ("irlfund/regenOS", "tok-11"))

# With only the first account configured, that same repo is announced-but-not-
# granted: skipped and silent, never halted. That is today's behaviour and it
# is why mirror-bot has needed the GitHub Action.
sync.INSTALL_TOKENS = sync.read_owner_tokens(
    {"GITHUB_OWNERS": "irlfund", "GITHUB_INSTALLATION_TOKEN_IRLFUND": "tok-11"})
pairs, ok = sync.discover()
check("one account configured: the other account's repo is skipped, not halted",
      "mirror-bot" not in [r for r, _, _, _ in pairs] and ok)

# Precedence, pinned because nothing else pins it. Swapping the two branches in
# discover_github() leaves every other check in this file green, and the flip is
# silent: the singular reaches one account, so that account's repos sync while
# the other's log as announced-but-not-granted and the run exits 0 logging `ok`.
# The launcher cannot produce both today - it withholds the singular whenever
# there are two or more owners - but a hand-run with a stale
# GITHUB_INSTALLATION_TOKEN still exported can, and that is the shape someone
# reaches for while debugging. Found by judgebot as a surviving mutant.
sync.INSTALL_TOKENS = sync.read_owner_tokens(ENV_2)
sync.INSTALL_TOKEN = "tok-11"
gh = sync.discover_github()
check("the owner list wins over a stale singular token, not the other way round",
      sorted(v[0] for v in gh.values()) ==
      ["gitcoinco/some-org-repo", "irlfund/agentic-engineering-infra",
       "irlfund/local-almanac", "irlfund/regenOS"])
sync.INSTALL_TOKEN = ""

# One repo reachable from two installations means two tokens disagree about who
# owns it. dict.update would keep whichever GitHub listed last.
sync.INSTALL_TOKENS = [("irlfund", "tok-11"), ("clone-of-irlfund", "tok-11")]
try:
    sync.discover_github()
    check("a repo granted in two installations fails the run", False)
except RuntimeError as e:
    check("a repo granted in two installations fails the run", True)
    check("and the message names a repo and says to revoke one grant",
          "irlfund/" in str(e) and "revoke" in str(e))

sync.INSTALL_TOKENS = []
sync.buzz_cli = lambda *a: json.dumps(BUZZ_REPOS_BY_OWNER.get(a[-1], []))

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

RUN_CALLS.clear()


def refused_then_ok(args, **kw):
    RUN_CALLS.append(args)
    if len(RUN_CALLS) < 3:
        raise RuntimeError(
            "git failed (128): fatal: unable to access "
            "'https://buzz.gitcoin.co/git/o/r.git/': Failed to connect to "
            "buzz.gitcoin.co port 443 after 0 ms: Connection refused")
    return "ok"


# A host restarting refuses connections. It was not retried until 2026-08-21,
# so a relay restart halted the mirror on the first failed operation.
sync.run = refused_then_ok
check("a refused connection is retried until it clears",
      sync.git("/tmp", "fetch") == "ok")
check("it took all three attempts", len(RUN_CALLS) == 3)

RUN_CALLS.clear()

# Discovery is the tick's first contact with the relay and it sits OUTSIDE
# tick()'s per-repo try, so a relay refusing at tick start never reaches halt():
# no state, no reason, no post, and no recovery message later either. The read
# is idempotent, so it retries.
sync.buzz_cli = REAL_BUZZ_CLI
sync.run = refused_then_ok
check("the retrying read wrapper works", sync.buzz_read("repos", "list") == "ok")
check("and it took all three attempts", len(RUN_CALLS) == 3)

RUN_CALLS.clear()


def refused_then_repos(args, **kw):
    RUN_CALLS.append(args)
    if len(RUN_CALLS) < 3:
        raise RuntimeError("Failed to connect to buzz.gitcoin.co port 443 after "
                           "0 ms: Connection refused")
    return json.dumps(BUZZ_REPOS_BY_OWNER.get(args[-1], []))


# The wrapper is worth nothing unless discovery actually goes through it. This
# is the wiring check: swapping buzz_read back to buzz_cli in discover_buzz()
# fails here and nowhere else.
sync.run = refused_then_repos
bz_retry, bz_retry_ok = sync.discover_buzz()
check("discovery itself survives a refusing relay",
      bz_retry_ok and bz_retry["regenos-dev"] == (OWNER1, "irlfund/regenOS"))

RUN_CALLS.clear()


def refused_always(args, **kw):
    RUN_CALLS.append(args)
    raise RuntimeError("Failed to connect to buzz.gitcoin.co port 443 after "
                       "0 ms: Connection refused")


# The writes must NOT retry: `messages send` and `pr open` are not idempotent,
# so a retry double-posts an alert or opens a second PR for the same sha.
sync.run = refused_always
try:
    sync.buzz_cli("messages", "send", "--channel", "c", "--content", "x")
    raised = False
except RuntimeError:
    raised = True
check("a write is not retried", raised and len(RUN_CALLS) == 1)

RUN_CALLS.clear()

# --- the CLI's own retryable flag -------------------------------------------
# A 5xx has three renderings and the pattern only ever knew two at a time. The
# CLI states its verdict, so for this one caller we read it instead of guessing
# at prose. run() attaches stderr, and the CLI prints its JSON there.
CLI_503 = ('buzz failed (2): {"error":"relay_error","message":"relay error 503: ",'
           '"retryable":true}')
CLI_404 = ('buzz failed (2): {"error":"relay_error","message":"relay error 404: '
           '404 page not found","retryable":false}')

check("the CLI's retryable flag is read", sync.cli_retryable(RuntimeError(CLI_503)) is True)
check("and its refusal is read too", sync.cli_retryable(RuntimeError(CLI_404)) is False)
check("no JSON means no verdict, so TRANSIENT decides",
      sync.cli_retryable(RuntimeError("buzz failed (101): panicked at 'x'")) is None)


def cli_503_then_ok(args, **kw):
    RUN_CALLS.append(args)
    if len(RUN_CALLS) < 3:
        raise RuntimeError(CLI_503)
    return "ok"


def cli_404_always(args, **kw):
    RUN_CALLS.append(args)
    raise RuntimeError(CLI_404)


sync.buzz_cli = REAL_BUZZ_CLI
sync.run = cli_503_then_ok
check("a relay 5xx is retried on the CLI's word",
      sync.buzz_read("repos", "list") == "ok" and len(RUN_CALLS) == 3)

RUN_CALLS.clear()

# The case where the CLI and the pattern DISAGREE, which is the only thing that
# proves buzz_read prefers the verdict. DNS: the CLI calls it retryable, the
# pattern does not match it, and the git side deliberately does not retry it.
# Deferring to the CLI here is a decision, not an oversight - see cli_retryable.
CLI_DNS = ('buzz failed (2): {"error":"network_error","message":"network error: '
           'dns error: failed to lookup address information","retryable":true}')


def cli_dns_then_ok(args, **kw):
    RUN_CALLS.append(args)
    if len(RUN_CALLS) < 3:
        raise RuntimeError(CLI_DNS)
    return "ok"


check("the pattern alone would not retry DNS",
      not sync.TRANSIENT.search(CLI_DNS))
sync.run = cli_dns_then_ok
check("but the CLI's verdict wins, so it is retried",
      sync.buzz_read("repos", "list") == "ok" and len(RUN_CALLS) == 3)

RUN_CALLS.clear()
sync.run = cli_404_always
try:
    sync.buzz_read("repos", "list")
    raised = False
except RuntimeError:
    raised = True
check("a relay 404 is not retried, also on the CLI's word",
      raised and len(RUN_CALLS) == 1)

# The regex is the fallback for when there is no JSON, so it needs the third
# spelling too. reqwest says `relay error 503`; curl and urllib say neither.
check("the third 5xx spelling is in the pattern",
      bool(sync.TRANSIENT.search("relay error 503: ")))

# `repository not found` is the relay's generic denial and a proxy with no
# backend renders the same way. The halt cannot resolve that, so it must say so.
check("an ambiguous 404 is flagged as ambiguous",
      bool(sync.REPO_NOT_FOUND.search(
          "git failed (128): remote: repository not found")))
check("an unrelated failure is not",
      not sync.REPO_NOT_FOUND.search("git failed (128): remote: requires Owner role"))

RUN_CALLS.clear()

# --- the GitHub half of discovery ------------------------------------------
# api() is called only from paginate() and the token mint, both inside
# discovery and neither inside reconcile(). So an API failure took the same
# path a refusing relay used to: out of tick(), no halt(), no post, no later
# recovery. It also means tick()'s `github-unavailable` branch is unreachable
# from the API - only git-over-https can raise into it.

import http.client  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

# urllib does not phrase a 5xx the way curl does, so the git-shaped half of
# TRANSIENT never matched here. This is the assertion that keeps both spellings.
check("urllib's 5xx wording is transient",
      bool(sync.TRANSIENT.search(str(
          urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)))))
check("curl's 5xx wording still is",
      bool(sync.TRANSIENT.search(
          "The requested URL returned error: 502")))
check("a 404 is not transient",
      not sync.TRANSIENT.search(str(
          urllib.error.HTTPError("u", 404, "Not Found", {}, None))))

sync.api = REAL_API
REAL_URLOPEN = urllib.request.urlopen
API_ATTEMPTS = []


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urlopen_502_then_ok(req, timeout=None):
    API_ATTEMPTS.append(req.full_url)
    if len(API_ATTEMPTS) < 3:
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)
    return FakeResp(b'{"ok": true}')


urllib.request.urlopen = urlopen_502_then_ok
check("a github api 5xx is retried until it clears",
      sync.api("/x", "tok") == {"ok": True})
check("and it took all three attempts", len(API_ATTEMPTS) == 3)

API_ATTEMPTS.clear()


def urlopen_disconnect_then_ok(req, timeout=None):
    API_ATTEMPTS.append(req.full_url)
    if len(API_ATTEMPTS) < 2:
        raise http.client.RemoteDisconnected(
            "Remote end closed connection without response")
    return FakeResp(b'{"ok": true}')


# RemoteDisconnected subclasses ConnectionResetError, so api()'s `except OSError`
# already caught it - but its message never says "reset", so TRANSIENT did not
# match and it raised on the first attempt. That is a far side restarting
# mid-request, which is the event this branch exists for.
urllib.request.urlopen = urlopen_disconnect_then_ok
check("a far side that hangs up mid-request is retried",
      sync.api("/x", "tok") == {"ok": True})
check("and it cleared on the second attempt", len(API_ATTEMPTS) == 2)

API_ATTEMPTS.clear()


def urlopen_truncated_then_ok(req, timeout=None):
    API_ATTEMPTS.append(req.full_url)
    if len(API_ATTEMPTS) < 2:
        raise http.client.IncompleteRead(b"short", 495)
    return FakeResp(b'{"ok": true}')


# The one that no pattern could have fixed. IncompleteRead subclasses
# HTTPException, not OSError, so before this it escaped api()'s handler
# entirely: out of tick(), exit 1, no halt and no post. Mutating the except
# clause back to `except OSError` fails this check by raising, not by looping.
urllib.request.urlopen = urlopen_truncated_then_ok
check("a truncated github response body is caught and retried",
      sync.api("/x", "tok") == {"ok": True})
check("and it cleared on the second attempt", len(API_ATTEMPTS) == 2)
check("urllib's truncation wording is transient",
      bool(sync.TRANSIENT.search(str(http.client.IncompleteRead(b"short", 495)))))

API_ATTEMPTS.clear()


def urlopen_bad_status_then_ok(req, timeout=None):
    API_ATTEMPTS.append(req.full_url)
    if len(API_ATTEMPTS) < 2:
        # Synthetic wording, and labelled as such: no BadStatusLine observed in
        # the wild words itself transiently, so the message is chosen to make
        # the two catch clauses disagree. What is being tested is the clause,
        # not the wording - the wordings are measured further down.
        raise http.client.BadStatusLine("relay error 503")
    return FakeResp(b'{"ok": true}')


# IncompleteRead was one member of a family. BadStatusLine, LineTooLong,
# InvalidURL and UnknownProtocol are HTTPException and not OSError too, so
# naming IncompleteRead alone left them escaping api() exactly as it did.
# BadStatusLine is what a proxy with no backend raises, which is producer four
# in AMBIGUOUS_404. Mutating the clause back to `except (OSError,
# http.client.IncompleteRead)` fails this by raising, not by looping.
urllib.request.urlopen = urlopen_bad_status_then_ok
check("a garbage status line is caught by the family, not by one subclass",
      sync.api("/x", "tok") == {"ok": True})
check("and it cleared on the second attempt", len(API_ATTEMPTS) == 2)
API_ATTEMPTS.clear()


def urlopen_bad_status_forever(req, timeout=None):
    API_ATTEMPTS.append(req.full_url)
    raise http.client.BadStatusLine("<html>hello</html>")


# The other half of the clause: catching wider is not retrying wider. A member
# whose wording is not transient still leaves on the first attempt, which is
# where it left this function before the clause was widened.
urllib.request.urlopen = urlopen_bad_status_forever
try:
    sync.api("/x", "tok")
    raised = False
except http.client.BadStatusLine:
    raised = True
check("a non-transient HTTPException still leaves on the first attempt",
      raised and len(API_ATTEMPTS) == 1)

API_ATTEMPTS.clear()

# git and urllib word the same two events differently, and git is the client
# that does all the mirror's real traffic. Both measured against a stub server
# on 2026-08-21 rather than quoted: a far side that accepts and hangs up, and
# one that answers and truncates.
check("git's wording for a hangup mid-request is transient",
      bool(sync.TRANSIENT.search(
          "fatal: unable to access 'https://buzz.gitcoin.co/git/o/r.git/': "
          "Empty reply from server")))
check("git's wording for a truncated body is transient",
      bool(sync.TRANSIENT.search(
          "fatal: unable to access 'https://github.com/o/r.git/': "
          "end of response with 495 bytes missing")))
# The one deliberately left out: a mid-stream push failure and a server-side
# hook rejection render identically, so retrying it would triple a real
# rejection.
check("a hung-up remote end is NOT transient",
      not sync.TRANSIENT.search(
          "fatal: the remote end hung up unexpectedly"))


def urlopen_404(req, timeout=None):
    API_ATTEMPTS.append(req.full_url)
    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)


# A 404 cannot clear on a second attempt, so it must halt immediately rather
# than spend GIT_TRIES * GIT_RETRY_DELAY seconds first.
urllib.request.urlopen = urlopen_404
try:
    sync.api("/x", "tok")
    raised = False
except urllib.error.HTTPError:
    raised = True
check("a github api 404 is not retried", raised and len(API_ATTEMPTS) == 1)

urllib.request.urlopen = REAL_URLOPEN
sync.api = fake_api

# Put the canned-JSON discovery stub back: everything below drives main(), which
# reaches discover_buzz(), and the real buzz_cli would shell out to `buzz`.
sync.buzz_cli = lambda *a: json.dumps(BUZZ_REPOS_BY_OWNER.get(a[-1], []))
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


def boom_refused(buzz_owner, repo_id, gh_repo, tok, st):
    raise RuntimeError("git failed (128): fatal: unable to access "
                       "'https://buzz.gitcoin.co/git/o/r.git/': Failed to "
                       "connect to buzz.gitcoin.co port 443 after 0 ms: "
                       "Connection refused")


# The relay restarting must not be reported as a revoked key. Before
# 2026-08-21 it was: connection-refused missed TRANSIENT, so `tick()` fell to
# the else arm and posted `buzz-auth-failed`. The halt is correct; the label
# is what a human reads at 3am, and these two want opposite responses.
sync.reconcile = boom_refused
POSTS.clear(); touched.clear()
check("a refused relay still fails the run", sync.main() == 1)
check("a relay restart is unavailable, not an auth failure",
      any("buzz-unavailable" in p for p in POSTS)
      and not any("auth-failed" in p for p in POSTS))


def boom_not_found(buzz_owner, repo_id, gh_repo, tok, st):
    raise RuntimeError("git failed (128): remote: repository not found\nfatal: "
                       "repository 'https://buzz.gitcoin.co/git/o/r.git/' not found")


# The wiring check for the ambiguity note: `repository not found` is the relay's
# generic denial, a stale coordinate and a proxy with no backend render the
# same, and none of the three carries a status code. tick() still has to label
# it `buzz-auth-failed` because the URL is a Buzz one, so the halt has to say
# that the label named one of three possibilities rather than a checked fact.
sync.reconcile = boom_not_found
POSTS.clear(); touched.clear()
check("an ambiguous 404 still halts", sync.main() == 1)
check("and the halt says the auth label was not a checked fact",
      any("ambiguous by design" in p for p in POSTS)
      and any("buzz channels members" in p for p in POSTS))
check("and it sends the reader to ls-remote, not to `buzz repos get`",
      any("git ls-remote" in p for p in POSTS)
      and any("not that a repo exists" in p for p in POSTS))
check("and it does NOT carry the GitHub note",
      not any("whether the installation covers it" in p for p in POSTS))


def boom_gh_not_found(buzz_owner, repo_id, gh_repo, tok, st):
    raise RuntimeError("git failed (128): remote: Repository not found.\nfatal: "
                       "repository 'https://github.com/irlfund/regenOS.git/' not found")


# GitHub words a missing App grant the same way the relay words its generic
# denial, so the note has to be gated on the label rather than on the wording.
# Reachable: the token is minted at discovery and used minutes later, so a repo
# deleted, renamed, made private, or a grant revoked inside that window lands
# here. Every step the note offers is about the relay, so on this halt all four
# are wrong, and its first line is false outright.
sync.reconcile = boom_gh_not_found
POSTS.clear(); touched.clear()
check("a GitHub 404 still halts", sync.main() == 1)
check("and it is labelled as a GitHub auth failure",
      any("github-auth-failed" in p for p in POSTS))
check("and it does NOT carry the relay's ambiguity note",
      not any("ambiguous by design" in p for p in POSTS))
check("and it carries the GitHub one instead",
      any("whether the installation covers it" in p for p in POSTS))
check("which does not send the reader to the relay",
      not any("buzz channels members" in p for p in POSTS))


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
