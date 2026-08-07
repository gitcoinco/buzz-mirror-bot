#!/usr/bin/env python3
"""buzz-mirror — fast-forward Buzz `main` onto GitHub `main`, one way.

Direction is **Buzz -> GitHub**. Buzz is origin; GitHub is a publish target that
Coolify happens to deploy from. It never rewrites history in either
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

**What gets mirrored is discovered, never configured.** The set is the
intersection of two opt-ins: a repo announced on Buzz under BUZZ_REPO_OWNER with
a GitHub `web` tag, and that same repo granted to the GitHub App. Adding a repo
is a tick in GitHub's install UI - not an env var edit and a redeploy, which is
exactly the per-repo cost this whole thing exists to remove.

Every tick is self-contained - state lives in state.json and the bare clones on
the volume, never in memory - so this runs either as a **Coolify scheduled task**
(`--once`, preferred) or as a long-running loop. Prefer `--once`: the run's own
exit status is the alert, and Coolify's Scheduled Task Failure notification fires
on it directly. No last-success file, no staleness probe, no second task watching
the first.

Coolify scheduled tasks `docker exec` into an already-running container
(`ScheduledTaskJob.php`: `docker exec {$containerName} sh -c '...'`) - they do
not start one, and Coolify has no host-level cron at all. So `--once` still needs
a container that stays up; the deployment runs `sleep infinity` as its main
process purely to be a target for the exec. Silly-looking, and worth it: a
non-zero exit is written as `status => 'failed'` and sends the team a `TaskFailed`
notification, and if the container is gone the exec itself fails the same way. So
one path alerts on both "the mirror broke" and "the mirror is not there".

The loop mode exists for when no scheduler is available. It needs more: a
`last-success` file, `healthcheck.sh`, and a separate staleness task - because a
wedged long-running process never exits to be noticed, and an alert that shares
a process with the thing it watches is not an alert.

Why polling and not the kind:30618 ref-state subscription: the relay does emit a
ref-state event on every push, and subscribing would cut latency from ~60s to
~2s. It also adds a persistent websocket, NIP-42 AUTH, reconnect handling, and a
spoofable input that must be treated as trigger-only. That is a poor trade for a
mirror whose consumer redeploys on its own schedule. The reconcile loop below is
the correctness mechanism either way; a subscription can be bolted on later as a
pure latency optimisation without touching it.
"""

import fcntl
import json
import os
import re
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

BUZZ_OWNER = os.environ["BUZZ_REPO_OWNER"]

# Optional allowlist of buzz repo-ids, for pointing a test deployment at one
# scratch repo. Unset means "everything discovery finds", which is the intended
# steady state - this is an escape hatch, not configuration.
ONLY = set(json.loads(os.environ.get("MIRROR_ONLY", "[]")))

if os.environ.get("MIRROR_REPOS"):
    # Fail rather than ignore it: silently dropping a var that used to decide
    # what gets mirrored is the kind of upgrade that looks fine for a week.
    raise SystemExit(
        "MIRROR_REPOS is gone - repos are discovered from the Buzz announcement's "
        "`web` tag plus the GitHub App's grants. Use MIRROR_ONLY to restrict."
    )


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
        # A corrupt state file must not wedge the mirror permanently. Losing it
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


def paginate(path, token, key=None):
    """GitHub caps list endpoints at 100 items. Three repos today, but the
    App's installation list is not something this controls - under "Any
    account" the install page is publicly reachable - so paging is not optional.
    """
    out, page = [], 1
    while page <= 50:  # a backstop, not a limit anything real should reach
        sep = "&" if "?" in path else "?"
        got = api(f"{path}{sep}per_page=100&page={page}", token)
        items = got[key] if key else got
        out.extend(items)
        if len(items) < 100:
            return out
        page += 1
    raise RuntimeError(f"{path}: more than 5000 items, refusing to page further")


def discover_github():
    """Every repo the App has actually been granted, with a token that reaches it.

    An App set to "Any account" gets a *separate installation per account* - a
    personal account and two orgs are three installations with three ids and
    three tokens, and a token is only valid for its own installation. So the
    unit of auth is the account, not the App. Installation ids are derivable
    from the PEM, so they are discovered rather than being values a human has
    to find and copy.

    One token is minted per installation, including ones nothing is used from.
    There is no App-JWT endpoint that lists an installation's repositories, so
    the token is the only way to see inside one; unwanted ones are dropped
    immediately. The per-account repo list is logged so a stray installation is
    visible rather than merely harmless.

    Returns {lowercased owner/name: (owner/name, token)}.
    """
    j = app_jwt()
    repos = {}
    for inst in paginate("/app/installations", j):
        acct = inst["account"]["login"]
        tok = api(
            f"/app/installations/{inst['id']}/access_tokens", j, method="POST"
        )["token"]
        granted = []
        for r in paginate("/installation/repositories", tok, key="repositories"):
            repos[r["full_name"].lower()] = (r["full_name"], tok)
            granted.append(r["name"])
        log(f"github: {acct} granted {len(granted)} repo(s): "
            f"{', '.join(sorted(granted)) or '(none)'}")
    return repos


# Trailing .git and a trailing slash are both things a human types into a web
# field; neither should change whether a repo is mirrored.
GITHUB_WEB = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+/[^/#?]+?)(?:\.git)?/?$")


def discover_buzz():
    """Which Buzz repo belongs to which GitHub repo, read off the announcement.

    A kind:30617 already carries `d` (the repo id the git endpoint is keyed by)
    and `web` (the human-facing URL, which for these is the GitHub repo). So the
    pairing is *declared* by whoever created the repo rather than guessed here.

    Guessing by name would have been wrong on two of the three repos that exist
    today: `regenos-dev` is `irlfund/regenOS` and `local-almanac-mirror` is
    `irlfund/local-almanac`. A repo with no GitHub `web` tag is not a mirror
    candidate and is ignored silently.

    Returns {buzz repo-id: owner/name}.
    """
    out = {}
    for ev in json.loads(buzz_cli("repos", "list", "--owner", BUZZ_OWNER)):
        tags = {}
        for t in ev.get("tags", []):
            if len(t) >= 2:
                tags.setdefault(t[0], t[1])
        m = GITHUB_WEB.match(tags.get("web", ""))
        if tags.get("d") and m:
            out[tags["d"]] = m.group(1)
    return out


def discover():
    """The mirror set: repos that opted in on *both* sides.

    Announced on Buzz with a GitHub `web` tag AND granted to the App. Requiring
    both is what makes an "Any account" App safe to leave open: a stranger who
    installs it gets nothing mirrored, because none of their repos are announced
    under BUZZ_REPO_OWNER.

    A mismatch on either side is logged, never halted. The GitHub grant is the
    enrolment action, so a Buzz repo nobody intends to mirror is not an error -
    and halting on it would alert forever. Logging both directions every run is
    what keeps a mis-click visible instead of silent.

    Returns [(repo_id, owner/name, token)].
    """
    gh, bz = discover_github(), discover_buzz()
    pairs, ungranted = [], []
    for repo_id, gh_repo in sorted(bz.items()):
        if ONLY and repo_id not in ONLY:
            continue
        hit = gh.get(gh_repo.lower())
        if hit:
            pairs.append((repo_id, hit[0], hit[1]))
            log(f"mirroring {repo_id} -> {hit[0]}")
        else:
            ungranted.append(f"{repo_id} -> {gh_repo}")

    if ungranted:
        log("skipped, announced on buzz but the App is not granted them: "
            + ", ".join(ungranted))
    claimed = {full.lower() for _, full, _ in pairs}
    unannounced = sorted(full for k, (full, _) in gh.items() if k not in claimed)
    if unannounced:
        log("skipped, granted to the App but not announced on buzz: "
            + ", ".join(unannounced))
    for missing in sorted(ONLY - set(bz)):
        log(f"WARN MIRROR_ONLY names {missing}, which has no buzz announcement")
    return pairs


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
    # Discovery re-runs every tick rather than being cached: installation tokens
    # expire in an hour anyway, so the listing is nearly free on top, and a repo
    # ticked in GitHub's UI should start mirroring on the next run with no
    # restart.
    pairs = discover()
    if not pairs:
        # Never intended in a working deployment, and silence would read exactly
        # like "everything is in sync". Loud on purpose.
        log("ERROR no repos to mirror - check the App's grants and the `web` tags")
        return False
    ok = True
    for repo_id, gh_repo, token in pairs:
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
    return ok


def hold_lock():
    """Take the run lock, or return None if another run already has it.

    Coolify does not dedupe scheduled runs: if a reconcile outlives the cron
    interval, the next `docker exec` starts regardless. Two runs sharing the
    bare clones and state.json would interleave fetches and let the last writer
    clobber halt state.

    An overlap is not a failure - the caller exits 0 - because paging someone
    about a slow tick is how an alert channel gets ignored.

    The returned handle must stay referenced for the lifetime of the run; the
    lock is released when it is closed or the process exits.
    """
    f = open(os.path.join(STATE_DIR, "lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def main():
    """Two run modes, because nothing here needs a process to stay alive.

    Every tick is self-contained: state lives in state.json and the bare clones
    on the volume, never in memory. So the loop is genuinely just a scheduler,
    and if the platform already has one, it should own the schedule instead.

      --once   run a single reconcile and exit non-zero if anything failed.
               Meant for a Coolify scheduled task, where the *failure itself*
               is the alert - no last-success file, no staleness probe, no
               second task watching the first. Note that Coolify execs into a
               running container, so this still needs one to be up; the
               deployment keeps `sleep infinity` as its main process for that.

      (default) loop forever. For when a scheduler is not available, or when a
               sub-minute interval is wanted. Liveness then needs the
               healthcheck + a separate staleness task, because a wedged
               long-running process never exits to be noticed.

    Prefer --once. It is strictly less machinery for the same behaviour.
    """
    os.makedirs(STATE_DIR, exist_ok=True)

    lock = hold_lock()
    if lock is None:
        log("another run holds the lock; nothing to do")
        return 0

    if "--once" in sys.argv:
        try:
            ok = tick()
        except Exception as e:
            # Discovery itself can fail - GitHub down, PEM revoked - and that is
            # not a per-repo halt. Exit non-zero so the scheduled task reports it.
            log(f"ERROR tick failed: {e}")
            return 1
        log("ok" if ok else "one or more repos halted")
        return 0 if ok else 1

    log(f"starting loop; interval={INTERVAL}s")
    while True:
        try:
            tick()
        except Exception as e:
            # Never die on a transient: the healthcheck decides we are
            # unhealthy, not a traceback on one bad fetch.
            log(f"ERROR tick failed: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
