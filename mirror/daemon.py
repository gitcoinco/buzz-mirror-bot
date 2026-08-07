#!/usr/bin/env python3
"""buzz-mirror-daemon — fast-forward Buzz `main` onto GitHub `main`, one way.

Direction is **Buzz -> GitHub**. Buzz is origin; GitHub is a publish target that
Coolify happens to deploy from. The daemon never rewrites history in either
direction: every push is a plain non-force push, and anything that is not a
clean fast-forward halts that repo instead of guessing.

Three outcomes per repo per tick:

  buzz == github          nothing to do
  github is ancestor      fast-forward GitHub to Buzz's tip
  buzz is ancestor        GitHub is AHEAD - the error case. Push GitHub's tip to
                          a `mirror/github-ahead-<sha>` branch on Buzz, open a
                          Buzz PR, post the one-line command to merge it, halt.
  neither                 diverged. Halt and say so. No automatic resolution.

The "GitHub ahead" path is deliberately **propose-only**. Pushing GitHub's tip
onto Buzz `main` would make GitHub a write path into Buzz, and mirror-bot cannot
push protected `main` anyway (the relay resolves push role from repo ownership
and channel role - a NIP-OA auth tag grants membership, not push rights). Every
other ref falls through to the Member default, which is why the proposal branch
works with no permission change.

Halts are **sticky**: one message per halt, not one per tick, and the halt holds
until the two tips converge. A halt freezes deploys for that repo, so the
message says so.

Liveness: a `last-success` file is touched after every fully successful tick.
`healthcheck.sh` reads it - used both as the container healthcheck (Coolify
restarts a wedged-but-running daemon) and as a Coolify scheduled task (Coolify
notifies out-of-band on Scheduled Task Failure). The daemon deliberately does
not alert on its own death: an alert that shares a process with the thing it
watches is not an alert.

Why polling and not the kind:30618 ref-state subscription: the relay does emit a
ref-state event on every push, and subscribing would cut latency from ~60s to
~2s. It also adds a persistent websocket, NIP-42 AUTH, reconnect handling, and a
spoofable input that must be treated as trigger-only. That is a poor trade for a
mirror whose consumer redeploys on its own schedule. The reconcile loop below is
the correctness mechanism either way; a subscription can be bolted on later as a
pure latency optimisation without touching it.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

import jwt  # PyJWT[crypto] - RS256 for the GitHub App assertion

RELAY = os.environ.get("BUZZ_RELAY_URL", "https://buzz.gitcoin.co")
GITHUB_API = "https://api.github.com"

STATE_DIR = os.environ.get("MIRROR_STATE_DIR", "/var/lib/git-mirror")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LAST_SUCCESS = os.path.join(STATE_DIR, "last-success")

INTERVAL = int(os.environ.get("MIRROR_INTERVAL_SECS", "60"))
CHANNEL = os.environ["MIRROR_ALERT_CHANNEL"]
PEM_PATH = os.environ.get("GITHUB_APP_PEM_PATH", "/run/mirror/ghapp.pem")
APP_ID = os.environ["GITHUB_APP_ID"]

# repo config: buzz repo-id -> GitHub owner/name. One line per repo, and adding a
# repo is a config change here plus an App installation click. Nothing lands in
# the mirrored repo itself - that per-repo cost is the whole reason this exists.
REPOS = json.loads(os.environ["MIRROR_REPOS"])
BUZZ_OWNER = os.environ["BUZZ_REPO_OWNER"]


def log(msg):
    """Unbuffered stdout - Coolify's log view is the only place these land."""
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


def run(args, **kw):
    """Run a command, raising with stderr attached. Never logs the environment,
    which carries both the Buzz key and the GitHub token."""
    p = subprocess.run(args, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed ({p.returncode}): {p.stderr.strip()}")
    return p.stdout.strip()


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # A corrupt state file must not wedge the daemon permanently. Losing it
        # costs at most one duplicate halt message.
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)  # atomic; a torn state file reads as corrupt


def touch_last_success():
    with open(LAST_SUCCESS, "w") as f:
        f.write(str(int(time.time())))


# --------------------------------------------------------------------------
# github app auth
# --------------------------------------------------------------------------


def app_jwt():
    """Sign the App assertion. `iat` is backdated 60s because GitHub rejects a
    JWT whose iat is in the future, and container clocks drift."""
    now = int(time.time())
    with open(PEM_PATH, "rb") as f:
        key = f.read()
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": APP_ID}, key, algorithm="RS256"
    )


def api(path, token, method="GET", scheme="Bearer"):
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        method=method,
        headers={
            "Authorization": f"{scheme} {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def installation_token():
    """Mint a fresh installation token. Discovering the installation id here
    rather than configuring it is deliberate - it is derivable from the PEM, so
    it should not be one more value a human has to find and copy."""
    j = app_jwt()
    installs = api("/app/installations", j)
    if not installs:
        raise RuntimeError("GitHub App is not installed on any account")
    if len(installs) > 1:
        # Ambiguous rather than wrong: pick nothing and say so.
        accounts = ", ".join(i["account"]["login"] for i in installs)
        raise RuntimeError(f"App installed on multiple accounts ({accounts}); pin one")
    tok = api(f"/app/installations/{installs[0]['id']}/access_tokens", j, method="POST")
    return tok["token"]


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

# The empty helper resets git's additive helper list; without it any ambient
# helper answers first with a username/password and the relay 401s. postBuffer
# is raised because git switches to chunked transfer above 1 MB and the relay
# rejects that with a misleading 401 + "Everything up-to-date".
GIT_COMMON = [
    "-c", "credential.helper=",
    "-c", "credential.helper=nostr",
    "-c", "credential.useHttpPath=true",
    "-c", "http.postBuffer=524288000",
]


def git(repo_dir, *args):
    return run(["git", "-C", repo_dir, *GIT_COMMON, *args])


def buzz_url(repo_id):
    return f"{RELAY}/git/{BUZZ_OWNER}/{repo_id}.git"


def github_url(gh_repo, token):
    """Built in one place on purpose: it carries a live installation token, so
    it must never end up in a log line, a remote entry, or an error message.
    Passing it per-invocation keeps it out of the repo's own config."""
    return f"https://x-access-token:{token}@github.com/{gh_repo}.git"


def ensure_repo(repo_id):
    """One bare repo per mirrored repo. Fetching both sides into it is what
    makes `merge-base --is-ancestor` answerable at all - ls-remote alone gives
    tips without the objects needed to relate them."""
    d = os.path.join(STATE_DIR, f"{repo_id}.git")
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        run(["git", "init", "--bare", "-q", d])
    return d


def fetch_both(d, repo_id, gh_repo, token):
    git(d, "fetch", "-q", "--no-tags", buzz_url(repo_id),
        "+refs/heads/main:refs/remotes/buzz/main")
    git(d, "fetch", "-q", "--no-tags", github_url(gh_repo, token),
        "+refs/heads/main:refs/remotes/github/main")
    return (git(d, "rev-parse", "refs/remotes/buzz/main"),
            git(d, "rev-parse", "refs/remotes/github/main"))


def is_ancestor(d, a, b):
    p = subprocess.run(["git", "-C", d, "merge-base", "--is-ancestor", a, b],
                       capture_output=True, text=True)
    return p.returncode == 0


# --------------------------------------------------------------------------
# buzz side effects
# --------------------------------------------------------------------------


def buzz_cli(*args):
    return run(["buzz", *args])


def post(text):
    """Channel post. Best-effort: a failure here must not abort the tick, or a
    Buzz outage would stop mirroring as well as reporting."""
    try:
        buzz_cli("messages", "send", "--channel", CHANNEL, "--content", text)
    except Exception as e:
        log(f"WARN could not post to buzz: {e}")


def open_pr(repo_id, branch, sha, gh_repo):
    try:
        out = buzz_cli(
            "pr", "open",
            "--repo-owner", BUZZ_OWNER,
            "--repo-id", repo_id,
            "--subject", f"GitHub is ahead of Buzz main ({sha[:7]})",
            "--commit", sha,
            "--clone", buzz_url(repo_id),
            "--branch-name", branch,
            "--channel", CHANNEL,
            "--body", (
                f"`{gh_repo}` main on GitHub is ahead of Buzz main.\n\n"
                f"Pushed as `{branch}`. Fast-forward Buzz main to adopt it:\n\n"
                f"```sh\ngit push buzz {sha}:refs/heads/main\n```\n"
            ),
        )
        return json.loads(out).get("link", "")
    except Exception as e:
        log(f"WARN could not open buzz PR: {e}")
        return ""


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------


def halt(state, repo_id, reason, detail):
    """Sticky by reason: re-post only when the reason changes, so a persistent
    halt is one message rather than one per tick. The message names the cost,
    because 'mirror halted' reads as cosmetic and it is not - Coolify deploys
    from GitHub, so a halt freezes this repo's deploys."""
    prev = state.get(repo_id, {})
    if prev.get("halted") == reason:
        return
    state[repo_id] = {"halted": reason, "since": int(time.time())}
    save_state(state)
    log(f"HALT {repo_id}: {reason} - {detail}")
    post(f"**Mirror halted: `{repo_id}`** (`{reason}`)\n\n{detail}\n\n"
         f"Deploys for this repo are frozen until this is resolved.")


def clear_halt(state, repo_id):
    if state.get(repo_id, {}).get("halted"):
        log(f"{repo_id}: halt cleared")
        post(f"Mirror recovered: `{repo_id}` is in sync again. Deploys unfrozen.")
    state[repo_id] = {}
    save_state(state)


def reconcile(repo_id, gh_repo, token, state):
    d = ensure_repo(repo_id)
    buzz_tip, gh_tip = fetch_both(d, repo_id, gh_repo, token)

    if buzz_tip == gh_tip:
        clear_halt(state, repo_id)
        return

    if is_ancestor(d, gh_tip, buzz_tip):
        # Buzz ahead: the normal case. Plain non-force push - even if this raced
        # with something, the server rejects a non-fast-forward, so main cannot
        # be rewritten from here.
        git(d, "push", github_url(gh_repo, token), f"{buzz_tip}:refs/heads/main")
        log(f"{repo_id}: {gh_tip[:7]} -> {buzz_tip[:7]}")
        clear_halt(state, repo_id)
        return

    if is_ancestor(d, buzz_tip, gh_tip):
        # GitHub ahead: propose, never push to Buzz main.
        branch = f"mirror/github-ahead-{gh_tip[:7]}"
        prev = state.get(repo_id, {})
        if prev.get("proposed") != gh_tip:
            git(d, "push", buzz_url(repo_id), f"{gh_tip}:refs/heads/{branch}")
            link = open_pr(repo_id, branch, gh_tip, gh_repo)
            state[repo_id] = {"halted": "github-ahead", "proposed": gh_tip,
                              "since": int(time.time())}
            save_state(state)
            log(f"HALT {repo_id}: github-ahead at {gh_tip[:7]}")
            post(
                f"**GitHub is ahead of Buzz on `{repo_id}`.**\n\n"
                f"Pushed `{branch}` to Buzz. Fast-forward main to adopt it:\n\n"
                f"```sh\ngit push buzz {gh_tip}:refs/heads/main\n```\n\n"
                + (f"{link}\n\n" if link else "")
                + "Deploys for this repo are frozen until this is resolved."
            )
        return

    halt(state, repo_id, "diverged",
         f"Buzz `{buzz_tip[:7]}` and GitHub `{gh_tip[:7]}` share no ancestry line. "
         f"Neither side can fast-forward to the other; this needs a human.")


def tick():
    state = load_state()
    token = installation_token()  # one token per tick; they expire in an hour
    ok = True
    for repo_id, gh_repo in REPOS.items():
        try:
            reconcile(repo_id, gh_repo, token, state)
        except Exception as e:
            ok = False
            # Name the subsystem. "Looks like a GitHub outage but is actually a
            # revoked Buzz key" was the failure mode worth spending a branch on.
            msg = str(e)
            if "buzz.gitcoin.co" in msg or "not a relay member" in msg:
                reason = "buzz-auth-failed"
            elif "github.com" in msg or "api.github.com" in msg:
                reason = "github-auth-failed"
            else:
                reason = "reconcile-failed"
            halt(state, repo_id, reason, f"```\n{msg[:800]}\n```")
    if ok:
        touch_last_success()


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    log(f"starting; repos={list(REPOS)} interval={INTERVAL}s")
    while True:
        try:
            tick()
        except Exception as e:
            # Never die on a transient: the restart would lose nothing but the
            # healthcheck is what should decide we are unhealthy, not a traceback.
            log(f"ERROR tick failed: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
