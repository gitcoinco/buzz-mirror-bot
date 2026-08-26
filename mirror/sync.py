#!/usr/bin/env python3
"""buzz-mirror — keep Buzz `main` and GitHub `main` on one line of history.

Buzz is origin; GitHub is a publish target that Coolify happens to deploy from,
so **Buzz -> GitHub is the normal direction**. It never rewrites history in
either direction: every push is a plain non-force push, and anything that is not
a clean fast-forward halts that repo instead of guessing.

Four outcomes per repo per tick:

  buzz == github          nothing to do
  github is ancestor      fast-forward GitHub to Buzz's tip
  buzz is ancestor        GitHub is ahead. Fast-forward *Buzz* to GitHub's tip
                          and say so in the channel. If the relay refuses the
                          push, fall back to proposing it (below).
  neither                 diverged. Halt and say so. No automatic resolution.

A strict ancestor is not a conflict in either direction - it is an update, and
adopting it needs no judgement. Reserving the alarm for *real* divergence is
what keeps the alarm worth reading: a repo that is legitimately developed
GitHub-first (the infra repo is) would otherwise sit in a permanent halt that
means nothing except "someone worked in the usual place for that repo".

Adopting the GitHub side does mean GitHub write reaches Buzz `main` with no
review step. That set already reaches production directly - Coolify deploys from
GitHub - so this grants it nothing it did not have. What is genuinely lost is
the nudge toward working Buzz-first, and the channel post is the replacement:
visible, not blocking.

It needs `push:member` on `refs/heads/main` in the repo's `buzz-protect` tags.
The relay takes `max(explicit push:role, default_min_role(ref, kind))` and its
built-in default for a fast-forward on a branch is Member, while non-fast-forward
and delete default to Admin and an explicit rule can never weaken them
(`buzz-core/src/git_perms.rs`). So that one tag means "mirror-bot may
fast-forward main and nothing else". Where it is *not* set the push is refused,
and the propose-only path below still runs - so this needs no flag day, and a
repo that should never be written from GitHub simply keeps `push:owner`.

Halts are **sticky**: one message per halt, not one per tick, and the halt holds
until the two tips converge. A halt freezes deploys for that repo, so the
message says so. Before a halt is ever raised, a transiently-failing git
operation (an HTTP 5xx, a reset connection, a timeout) is retried a couple of
times inside the tick - see git() - so a blip that clears in seconds does not
page anyone.

**What gets mirrored is discovered, never configured.** The set is the
intersection of two opt-ins: a repo announced on Buzz by a pubkey in
BUZZ_REPO_OWNER (comma-separated allowlist of announcing keys) with a GitHub
`web` tag, and that same repo granted to the GitHub App. Adding a repo
is a tick in GitHub's install UI - not an env var edit and a redeploy, which is
exactly the per-repo cost this whole thing exists to remove.

The App grant is the *enrolment act*, and it is not divisible: granted means
fully enrolled, so the mirror pushes the repo and the fleet's dispatcher builds
it. Those are two halves of one pipeline rather than two things to opt into
separately, which is why there is no allowlist here to narrow the set with.

Every tick is self-contained - state lives in state.json and the bare clones on
the volume, never in memory - so nothing needs a process to stay alive. The
deployed shape is one container per run: a **systemd timer on infra-box** runs
`docker run --rm <image> --once` every minute (it was five until 2026-08-09; the
units live in the aei repo, not here). The run's own
exit status is the alert, via the unit's `OnFailure=`.

Two things follow from running there rather than as a Coolify application. The
token issuer is local, so the mirror is handed an installation token that
expires in an hour instead of carrying the App's PEM - see discover_github().
And systemd will not start a second instance of a unit that is already active,
so overlapping runs cannot happen; the flock below stays anyway, because a
hand-run `docker run` alongside a timed one still can.

A timer that stops firing produces no failure to hook, so `OnFailure=` cannot
see it. That is what `last-success` is for: fleet-audit asserts on its age from
outside this process, which is the only place an alert about a dead mirror can
honestly live.

The loop mode exists for anywhere without a scheduler. It needs more: the same
`last-success` file, `healthcheck.sh`, and a separate staleness check - because
a wedged long-running process never exits to be noticed.

Why polling and not the kind:30618 ref-state subscription: the relay does emit a
ref-state event on every push, and subscribing would cut latency from ~60s to
~2s. It also adds a persistent websocket, NIP-42 AUTH, reconnect handling, and a
spoofable input that must be treated as trigger-only. That is a poor trade for a
mirror whose consumer redeploys on its own schedule. The reconcile loop below is
the correctness mechanism either way; a subscription can be bolted on later as a
pure latency optimisation without touching it.
"""

import fcntl
import http.client
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
# Only needed on the PEM path; a deployment with an issuer sets neither.
PEM_PATH = os.environ.get("GITHUB_APP_PEM_PATH", "/run/mirror/ghapp.pem")
APP_ID = os.environ.get("GITHUB_APP_ID", "")

# One or more announcing pubkeys, comma-separated. Multiple owners exist
# because repo ids are reserved to their creating key forever: repos announced
# by different daemon/agent keys (planbot, repobot) can never be re-announced
# under one key, so the mirror follows an allowlist of keys instead. The App
# grant stays the enrolment act; this list only says whose announcements count.
BUZZ_OWNERS = [o.strip().lower() for o in os.environ["BUZZ_REPO_OWNER"].split(",") if o.strip()]
if not BUZZ_OWNERS:
    raise SystemExit("BUZZ_REPO_OWNER is empty")
for _o in BUZZ_OWNERS:
    if not re.fullmatch(r"[0-9a-f]{64}", _o):
        raise SystemExit(f"BUZZ_REPO_OWNER entry is not a 64-char hex pubkey: {_o!r}")

# An installation token from the issuer on infra-box, if the deployment has one.
# Preferred over the PEM: the mirror then holds a credential that expires in an
# hour instead of one that does not expire at all. It must be
# installation-WIDE, not narrowed to a repo list - see discover_github().
INSTALL_TOKEN = os.environ.get("GITHUB_INSTALLATION_TOKEN", "")


def token_var(owner):
    """The env var carrying one account's installation token.

    Uppercased, with every character an env var name cannot hold replaced by
    `_`: GitHub account names allow hyphens and env var names do not. aei's
    host/buzz-mirror-run-once.sh derives the same name from the same owner list
    before it mints anything, so this rule is a contract across two repos.
    Change it on one side and that account arrives with no credential.
    """
    return "GITHUB_INSTALLATION_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", owner.upper())


def read_owner_tokens(environ):
    """[(owner, token)] from GITHUB_OWNERS, or [] when it is unset.

    One token per GitHub account, because a token is only ever valid for ONE
    installation and the fleet's repos are not all in one account: the mirrored
    repos are granted under irlfund, and this mirror's own GitHub half lives
    under gitcoinco. Its enrolment set is the union of the grants.

    Separators are commas and/or whitespace, matching BUZZ_REPO_OWNER above -
    the file this comes from on infra-box is *sourced as bash*, where an
    unquoted space is a syntax error, so the comma form is the one to write and
    the whitespace form is accepted so the quoted spelling is not a trap.

    A named account with no token is fatal rather than skipped. Skipping it
    would drop every repo in that account out of the mirror set and log a
    perfectly clean run, which is the silent stop this daemon exists to prevent.
    """
    owners, seen = [], set()
    for o in re.split(r"[,\s]+", environ.get("GITHUB_OWNERS", "").strip()):
        if not o or o.lower() in seen:
            continue  # a repeat is a typo; one token per account either way
        seen.add(o.lower())
        owners.append(o)

    out = []
    for o in owners:
        var = token_var(o)
        tok = environ.get(var, "")
        if not tok:
            raise SystemExit(
                f"GITHUB_OWNERS names {o!r} but {var} is empty - the caller "
                f"mints one installation-wide token per account and passes it "
                f"in under that name (see aei host/buzz-mirror-run-once.sh)"
            )
        out.append((o, tok))
    return out


INSTALL_TOKENS = read_owner_tokens(os.environ)

for gone, why in (
    ("MIRROR_REPOS", "repos are discovered from the Buzz announcement's `web` tag "
                     "plus the GitHub App's grants"),
    ("MIRROR_ONLY", "enrolment is the App grant and nothing else: granted means "
                    "fully enrolled, in both directions. To mirror one scratch "
                    "repo, grant the App one scratch repo"),
):
    # Fail rather than ignore: silently dropping a var that used to decide what
    # gets mirrored is the kind of upgrade that looks fine for a week.
    if os.environ.get(gone):
        raise SystemExit(f"{gone} is gone - {why}.")


def log(msg):
    """Unbuffered stdout - it is the run's journal entry and nothing else."""
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
    if not APP_ID:
        raise RuntimeError(
            "no GitHub credential: set GITHUB_INSTALLATION_TOKEN (from the "
            "issuer on infra-box), or GITHUB_APP_ID plus a PEM"
        )
    now = int(time.time())
    with open(PEM_PATH, "rb") as f:
        key = f.read()
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": APP_ID}, key, algorithm="RS256"
    )


def api(path, token, method="GET", scheme="Bearer"):
    """A GitHub API call, retried on a transient failure like git() and
    buzz_read().

    Both call sites are in discovery - the installation listing in paginate()
    and the token mint below - and neither is in reconcile(). So a GitHub 5xx
    here takes the same path a refusing relay used to: straight out of tick(),
    `ERROR tick failed`, exit 1, no halt() and therefore no post and no later
    recovery message. It also means the `github-unavailable` branch in tick()
    is unreachable from the API; only git-over-https can raise into it.

    The token mint is a POST, and it is retried anyway: it creates a fresh
    short-lived installation token, so a duplicate is an unused token that
    expires in an hour, not a duplicated side effect. That is the reason
    buzz_read exists for the relay's reads but its writes are left alone -
    `messages send` and `pr open` are not harmless to repeat.

    urllib.error.HTTPError and URLError both subclass OSError; a malformed body
    raises ValueError and is not retried, which is right - it will not clear.

    http.client.HTTPException is the exception to that tidy story. It does NOT
    subclass OSError, so every member of that family escaped this handler
    entirely - out of tick(), `ERROR tick failed`, exit 1, no halt, no post. A
    truncated response body (IncompleteRead) is the one that started this, but
    naming it alone left BadStatusLine, LineTooLong, InvalidURL and
    UnknownProtocol still escaping. Any of them is the same event as the one
    that started this: api.github.com answered badly or not at all. The family
    is caught, not one member of it, because no pattern can help an exception
    that is never caught.

    An earlier version of this paragraph cited Coolify's proxy with no backend
    as the BadStatusLine case. That example is wrong twice, and opsbot caught
    both: the proxy is producer THREE in AMBIGUOUS_404, not four, and it cannot
    reach this function at all. The one urlopen below goes to GITHUB_API; the
    relay is reached through the buzz CLI as a subprocess and through git, so a
    proxy answering with garbage arrives as a RuntimeError out of run(), never
    as an HTTPException. Justifying a handler with a failure from the other
    side of the system is the mirror of the bug this branch keeps finding.

    Catching wider does not retry wider: TRANSIENT still decides, so a member
    whose wording is not transient re-raises on the first attempt, which is
    where it left this function before. Retrying the ones that do match is safe
    on the same grounds as everything else here: the listing is a GET and the
    mint is idempotent in effect.
    """
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        method=method,
        headers={
            "Authorization": f"{scheme} {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(1, GIT_TRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except (OSError, http.client.HTTPException) as e:
            if attempt == GIT_TRIES or not TRANSIENT.search(str(e)):
                raise
            log(f"WARN transient github api failure (attempt {attempt}/"
                f"{GIT_TRIES}), retrying in {GIT_RETRY_DELAY}s: {str(e)[:200]}")
            time.sleep(GIT_RETRY_DELAY)


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


def repos_for_token(tok, label):
    """The repos one installation token reaches, keyed for case-insensitive
    lookup. Logged, so a stray grant is visible rather than merely harmless."""
    repos, granted = {}, []
    for r in paginate("/installation/repositories", tok, key="repositories"):
        repos[r["full_name"].lower()] = (r["full_name"], tok)
        granted.append(r["name"])
    log(f"github: {label} granted {len(granted)} repo(s): "
        f"{', '.join(sorted(granted)) or '(none)'}")
    return repos


def merge_grants(into, more, label):
    """Union one installation's grants into the set, refusing a repeat.

    Not `dict.update`: that keeps the last token seen and says nothing. A repo
    reachable from two installations means the two tokens disagree about who
    owns it, and picking one would make the mirror's behaviour depend on the
    order GitHub happened to list the installations in. There is no safe guess,
    so the run fails and a human looks.

    Ordinarily unreachable - a repo lives under exactly one account - but a
    transfer between two accounts the App is installed on can leave both
    installations claiming it for a while.
    """
    for key, val in more.items():
        if key in into:
            raise RuntimeError(
                f"{val[0]} is granted in two installations (the second is "
                f"{label}) - one repo, two tokens, no way to tell which is "
                f"current; revoke the grant on the account that no longer "
                f"owns it"
            )
        into[key] = val
    return into


def discover_github():
    """Every repo the App has actually been granted, with a token that reaches it.

    Three ways in, and the deployed one is the owner list.

    **GITHUB_OWNERS plus one GITHUB_INSTALLATION_TOKEN_<OWNER> each** - the
    issuer on infra-box mints one installation-wide token per named account and
    the caller passes them in. A GitHub App token is only ever valid for ONE
    installation, so an App installed on several accounts needs several tokens;
    the mirror set is their union. This is what lets the mirror carry a repo
    outside the account holding everything else - see docs/MIRROR.md.

    **GITHUB_INSTALLATION_TOKEN**, one token, one account. The original shape,
    kept working because this image and aei's launcher are separate artifacts
    that deploy independently: whichever lands first, the tick still runs.

    Either way the token must be installation-WIDE: the issuer can narrow a
    token to a repo list, and doing that here would quietly move enrolment out
    of the App grant and into the issuer's flags, which is exactly the thing
    enrolment is not allowed to be. Granted means fully enrolled; this function
    is how that is read.

    **The PEM**, for a deployment with no issuer (loop mode, a laptop). An App
    set to "Any account" gets a *separate installation per account*, each with
    its own id and its own token, and a token is only valid for its own
    installation - so the unit of auth is the account, not the App. Installation
    ids are derivable from the PEM, so they are discovered rather than being
    values a human has to find and copy. There is no App-JWT endpoint that lists
    an installation's repositories, so a token per installation is the only way
    to see inside one. This path finds every installation; the two token paths
    above see only the accounts they were given, which is the point of naming
    them.

    Returns {lowercased owner/name: (owner/name, token)}.
    """
    if INSTALL_TOKENS:
        repos = {}
        for owner, tok in INSTALL_TOKENS:
            merge_grants(repos, repos_for_token(tok, f"{owner} installation token"), owner)
        return repos

    if INSTALL_TOKEN:
        return repos_for_token(INSTALL_TOKEN, "installation token")

    j = app_jwt()
    repos = {}
    for inst in paginate("/app/installations", j):
        tok = api(
            f"/app/installations/{inst['id']}/access_tokens", j, method="POST"
        )["token"]
        login = inst["account"]["login"]
        merge_grants(repos, repos_for_token(tok, login), login)
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

    Returns ({buzz repo-id: (buzz_owner, owner/name)}, ok).
    """
    out, owner_of, ok = {}, {}, True
    for buzz_owner in BUZZ_OWNERS:
        for ev in json.loads(buzz_read("repos", "list", "--owner", buzz_owner)):
            tags = {}
            for t in ev.get("tags", []):
                if len(t) >= 2:
                    tags.setdefault(t[0], t[1])
            m = GITHUB_WEB.match(tags.get("web", ""))
            if not (tags.get("d") and m):
                continue
            repo_id = tags["d"]
            if repo_id in owner_of and owner_of[repo_id] != buzz_owner:
                # Reachable: the relay accepts a second announcement of a
                # reserved id (the reservation fails as a post-ingest side
                # effect, so the event stores and lists but the repo behind
                # it 404s forever). Mirroring either would guess which is
                # real, so drop both - and fail the run, or a repo that was
                # mirroring fine would stop mirroring with exit 0.
                log(f"ERROR repo id {repo_id} announced by two allowlisted "
                    f"owners - skipping it entirely")
                out.pop(repo_id, None)
                ok = False
                continue
            owner_of[repo_id] = buzz_owner
            out[repo_id] = (buzz_owner, m.group(1))
    return out, ok


def discover():
    """The mirror set: repos that opted in on *both* sides.

    Announced on Buzz with a GitHub `web` tag AND granted to the App. Requiring
    both is what makes an "Any account" App safe to leave open: a stranger who
    installs it gets nothing mirrored, because none of their repos are announced
    by an allowlisted key in BUZZ_REPO_OWNER.

    A mismatch on either side is logged, never halted. The GitHub grant is the
    enrolment action, so a Buzz repo nobody intends to mirror is not an error -
    and halting on it would alert forever. Logging both directions every run is
    what keeps a mis-click visible instead of silent.

    Two Buzz repos pointing at the *same* GitHub repo is different: they would
    take turns fast-forwarding one GitHub main from unrelated histories, so the
    mirror would thrash forever and the repo would effectively be corrupt. There
    is no safe guess about which is authoritative, so both are dropped and the
    run fails.

    Returns ([(repo_id, buzz_owner, owner/name, token)], ok).
    """
    gh, (bz, bz_ok) = discover_github(), discover_buzz()

    # Collisions are resolved before anything is mirrored, not while iterating:
    # dropping the second one seen would make the winner depend on sort order.
    claims = {}
    for repo_id, (_, gh_repo) in sorted(bz.items()):
        claims.setdefault(gh_repo.lower(), []).append(repo_id)
    contested = {k: v for k, v in claims.items() if len(v) > 1}

    ok = bz_ok
    pairs, ungranted = [], []
    for repo_id, (buzz_owner, gh_repo) in sorted(bz.items()):
        if gh_repo.lower() in contested:
            continue
        hit = gh.get(gh_repo.lower())
        if hit:
            pairs.append((repo_id, buzz_owner, hit[0], hit[1]))
            log(f"mirroring {repo_id} -> {hit[0]}")
        else:
            ungranted.append(f"{repo_id} -> {gh_repo}")

    for gh_repo, ids in sorted(contested.items()):
        ok = False
        log(f"ERROR {gh_repo} is claimed by {len(ids)} buzz repos ({', '.join(ids)}) - "
            f"skipping all of them; fix the `web` tags so each points at its own repo")

    if ungranted:
        log("skipped, announced on buzz but the App is not granted them: "
            + ", ".join(ungranted))
    claimed = {full.lower() for _, _, full, _ in pairs}
    unannounced = sorted(full for k, (full, _) in gh.items() if k not in claimed)
    if unannounced:
        log("skipped, granted to the App but not announced on buzz: "
            + ", ".join(unannounced))
    return pairs, ok


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


# Failure classes worth a second attempt within the tick: a 5xx from either
# git host, a reset connection, a timeout, a refused or unreachable host. Auth
# denials and non-fast-forward rejections are deliberately absent - retrying
# those cannot succeed and would only delay the halt.
#
# A host that is restarting refuses connections, and until 2026-08-21 that was
# neither retried nor labelled honestly: `tick()` reads this pattern to choose
# between `*-unavailable` and `*-auth-failed`, so a relay restart was reported
# as `buzz-auth-failed` - the label for a revoked key, which is the one thing
# the branch there exists to keep apart from an outage. Both halves are fixed
# by matching it here.
#
# `failed to connect` also covers unreachable and no-route. It can match a
# permanently wrong host or port too; the cost of that is one halt delayed by
# GIT_TRIES * GIT_RETRY_DELAY seconds, which is cheaper than mislabelling an
# outage as an auth failure.
#
# THREE spellings of the same 5xx, because three clients report it and each
# words it differently:
#
#   returned error: 503   curl, via git
#   HTTP Error 503        urllib, via api()
#   relay error 503       reqwest, via the buzz CLI
#
# Matching only curl's is how this pattern silently did nothing on the GitHub
# API side. Adding urllib's and stopping there is how it would silently have
# done nothing on the relay side. Measured by opsbot against a live relay,
# 2026-08-21. If a fourth client ever reports here, assume its wording is a
# fourth spelling until someone has run it.
#
# `closed connection without response` is http.client.RemoteDisconnected, which
# subclasses ConnectionResetError and so is already caught in api() - but its
# message never says "reset", so it raised on the first attempt. A relay or
# GitHub restart mid-request is exactly the event this pattern exists for.
# Found by judgebot, 2026-08-21; the string is measured, not quoted from docs.
#
# Five review rounds added one spelling each, so the sixth and seventh came
# from running the ENUMERATION instead: drive every client this pattern reads
# for against the same two events, and write down what each one says. Stub
# server, measured 2026-08-21, `curl 7.88` / `git 2.47` / `python 3.13`:
#
#   far side accepts the request and hangs up without answering
#     git     unable to access '...': Empty reply from server
#     urllib  Remote end closed connection without response
#     CLI     Connection reset by peer (os error 104), retryable:true
#
#   far side answers, then truncates the body
#     git     end of response with 495 bytes missing
#     urllib  IncompleteRead(5 bytes read, 495 more expected)
#
# Note which half was uncovered: git, the client that does all the mirror's
# real traffic, on the event judgebot found on the urllib side. `Empty reply
# from server` carries no status code and does not say "reset", so with
# buzz.gitcoin.co in the URL it landed on `buzz-auth-failed` - the 13:34
# mislabel again, arriving by the busiest path. Found by opsbot, 2026-08-21.
#
# The truncation pair is worse than a missing spelling: IncompleteRead
# subclasses http.client.HTTPException, NOT OSError, so api() did not catch it
# at all and no pattern could have helped. Fixed there; see api().
#
# NOT in, and deliberately: `the remote end hung up unexpectedly`. A push that
# dies mid-stream renders that way, but so does a server-side hook that
# rejects, and retrying that triples a real rejection. Nobody has reproduced it
# cleanly against a stub, so it stays out until someone has.
#
# The relay half is belt-and-braces: buzz_read prefers the CLI's own
# `retryable` field and only falls back to this pattern when there is no JSON
# to read. See cli_retryable().
TRANSIENT = re.compile(
    r"returned error: 5\d\d|HTTP Error 5\d\d|relay error 5\d\d"
    r"|connection reset|closed connection without response|timed out"
    r"|connection refused|failed to connect"
    r"|empty reply from server|end of response with \d+ bytes missing"
    r"|incompleteread",
    re.IGNORECASE)

# Named for git because that is where they started; they now also govern the
# relay read in discover_buzz(). Kept as one pair on purpose: both are the same
# question, "did the far side answer this tick".
# `repository not found` is the relay's GENERIC DENIAL: a coordinate that does
# not resolve, and a caller who is not a member of the repo's bound channel,
# are deliberately rendered identically. Coolify's proxy with no backend is a
# third producer, and an announcement whose repo was never created is a fourth.
# None of the four carries a status code, and because the URL contains
# buzz.gitcoin.co, tick() lands all of them on `buzz-auth-failed`, which names
# one.
#
# The fourth is the one that defeats a naive check, and it is why step 1 below
# is ls-remote rather than `buzz repos get`. ingest_event() inserts the event
# BEFORE running its side effects and only logs a side-effect failure
# (handlers/ingest.rs, "the event was accepted but its side effects ... did not
# run"). So a kind:30617 whose handler errored - name taken by another owner,
# per-pubkey repo limit, manifest pointer failure - is stored, listed, and
# returns a matching `clone` tag for a repo that does not exist. Announcement
# resolution is not liveness. `git ls-remote` is the only thing that is.
#
# The ambiguity is not fixable from this side, so the halt says so instead. Two
# agents spent an hour on it from opposite ends on 2026-08-21, each confidently
# naming a different one. opsbot found the fourth while reviewing this.
REPO_NOT_FOUND = re.compile(r"repository .*not found", re.IGNORECASE)
AMBIGUOUS_404 = (
    "\n\n`repository not found` is ambiguous by design. It means one of: the "
    "repo id or owner has changed and this remote is stale; this key is not a "
    "member of the repo's bound channel; the announcement exists but the repo "
    "behind it never did; or the relay is up and its proxy has no backend. The "
    "label above says auth because the URL is a Buzz one, not because the key "
    "was checked.\n\n"
    "In that order of cost to check:\n"
    "1. `git ls-remote <clone tag>` - NOT `buzz repos get`. A stored "
    "announcement proves an event was accepted, not that a repo exists: the "
    "relay inserts the event before its side effects and only logs a failure, "
    "so a matching `clone` tag can front nothing at all.\n"
    "2. compare that `clone` tag to the remote actually configured here\n"
    "3. `buzz channels members --channel <the announcement's buzz-channel>`\n"
    "4. whether the relay was restarting at this timestamp"
)

# GitHub words a missing App grant the same way, so the note above is gated on
# the label. Gating it left the GitHub reader with a bare traceback and no
# guidance, and the guidance already existed - in a test comment, which is the
# one place an operator at 3am will not look. Same four words, entirely
# different answer. opsbot asked for this while reviewing.
GITHUB_404 = (
    "\n\nGitHub renders both a missing repo and a missing App grant as "
    "`Repository not found.`, so this does not say which. The token is minted "
    "during discovery and used minutes later, so the repo may have been "
    "deleted, renamed or made private in that window, or the App's grant "
    "revoked in it.\n\n"
    "Check, in that order:\n"
    "1. whether the repo still exists under that owner and name\n"
    "2. whether the installation covers it - if it is set to selected "
    "repositories, one added since the grant is not in it; if it is set to all "
    "repositories, new ones are picked up and this is not your cause\n"
    "3. whether the repo went private\n\n"
    "None of the relay's checks apply here. The mirror's Buzz side is fine."
)

GIT_TRIES = 3        # attempts per network operation within one tick
GIT_RETRY_DELAY = 5  # seconds between attempts


def git(repo_dir, *args):
    """Git with the mirror's config, retrying transient network failures.

    Without this, each git operation ran exactly once per tick, so a single
    GitHub 500 that cleared itself in seconds still halted the repo and paged
    the alert channel until the next tick recovered it. A short in-tick retry
    absorbs those; anything that survives all attempts is worth the halt."""
    for attempt in range(1, GIT_TRIES + 1):
        try:
            return run(["git", "-C", repo_dir, *GIT_COMMON, *args])
        except RuntimeError as e:
            if attempt == GIT_TRIES or not TRANSIENT.search(str(e)):
                raise
            log(f"WARN transient git failure (attempt {attempt}/{GIT_TRIES}), "
                f"retrying in {GIT_RETRY_DELAY}s: {str(e)[:200]}")
            time.sleep(GIT_RETRY_DELAY)


def buzz_url(buzz_owner, repo_id):
    return f"{RELAY}/git/{buzz_owner}/{repo_id}.git"


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


def remote_main(d, url, side):
    """Fetch one side's main and return (tip_sha, head_count).

    tip_sha is None when the remote has no `main` - a repo announced before its
    first push, or one whose default branch is named something else. head_count
    tells those apart: 0 is an empty repo, more means branches exist but none
    of them is `main`. The listing goes first because fetching a missing ref is
    an error, and one indistinguishable from a typo'd refspec."""
    heads = [line.split("\t", 1)[1] for line in
             git(d, "ls-remote", "--heads", url).splitlines() if "\t" in line]
    if "refs/heads/main" not in heads:
        # Drop any tracking ref from an earlier tick, or a ref deleted on the
        # remote would keep its last known tip here forever.
        subprocess.run(["git", "-C", d, "update-ref", "-d",
                        f"refs/remotes/{side}/main"], capture_output=True)
        return None, len(heads)
    git(d, "fetch", "-q", "--no-tags", url,
        f"+refs/heads/main:refs/remotes/{side}/main")
    return git(d, "rev-parse", f"refs/remotes/{side}/main"), len(heads)


def fetch_both(d, buzz_owner, repo_id, gh_repo, token):
    return (remote_main(d, buzz_url(buzz_owner, repo_id), "buzz"),
            remote_main(d, github_url(gh_repo, token), "github"))


def is_ancestor(d, a, b):
    p = subprocess.run(["git", "-C", d, "merge-base", "--is-ancestor", a, b],
                       capture_output=True, text=True)
    return p.returncode == 0


# --------------------------------------------------------------------------
# buzz side effects
# --------------------------------------------------------------------------


def buzz_cli(*args):
    return run(["buzz", *args])


def cli_retryable(err):
    """The buzz CLI's own verdict on its failure, or None if it did not give one.

    The CLI prints a structured error to stderr and run() attaches stderr, so
    the JSON is inside the RuntimeError text:

        {"error":"relay_error","message":"relay error 503: ","retryable":true}

    Preferred over TRANSIENT for this one caller because it is a stated contract
    rather than a guess at prose. The prose guess is genuinely fragile here: a
    5xx has three renderings depending on which client saw it - `returned error:
    503` from curl through git, `HTTP Error 503` from urllib in api(), and
    `relay error 503` from reqwest in the CLI. Two of those were in the pattern
    and the third was not, which is the hole opsbot measured on 2026-08-21.

    Deferring to `retryable` wholesale, including for DNS, which the CLI reports
    as retryable and the git side does not retry. The two paths disagree on
    purpose: on the git side there is no verdict to read and prose is all there
    is, so `could not resolve host` is excluded to keep a permanently wrong host
    from costing GIT_TRIES * GIT_RETRY_DELAY on every tick. Here the CLI has
    already answered, and a carve-out would mean overriding its contract to
    save ten seconds on a misconfiguration. If that turns out to be wrong, the
    carve-out belongs here as one explicit branch, not as a change of policy.

    Returns None when there is no JSON to read - a panic, a truncated stream -
    and the caller falls back to TRANSIENT.
    """
    text = str(err)
    lo, hi = text.find("{"), text.rfind("}")
    if lo == -1 or hi < lo:
        return None
    try:
        return json.loads(text[lo:hi + 1]).get("retryable")
    except (ValueError, AttributeError):
        return None


def buzz_read(*args):
    """A READ-ONLY buzz call, retried like git on a transient relay failure.

    Deliberately separate from buzz_cli rather than folded into it: the writes
    that go through buzz_cli are `messages send` and `pr open`, and neither is
    idempotent. Retrying those would double-post an alert or open a second PR
    for the same sha, which is worse than the failure.

    This exists because discovery is the tick's FIRST contact with the relay and
    it sits outside tick()'s per-repo `try` (`discover()` is called at the top of
    tick(), and main() catches what escapes). A relay that is already refusing
    when the tick starts therefore never reaches halt(): no state entry, no
    reason, no post - and because nothing was halted, the next good tick has no
    halt to clear, so there is no recovery message either. The whole outage
    leaves one `ERROR tick failed` line per tick and nothing structured.

    A restart that clears inside GIT_TRIES * GIT_RETRY_DELAY now costs nothing
    at all. A longer one still lands there; that shape is aei issue 9230a23d.

    Retries on the CLI's own `retryable` flag where it gives one, falling back
    to TRANSIENT where it does not - see cli_retryable().
    """
    for attempt in range(1, GIT_TRIES + 1):
        try:
            return buzz_cli(*args)
        except RuntimeError as e:
            verdict = cli_retryable(e)
            if verdict is None:
                verdict = bool(TRANSIENT.search(str(e)))
            if attempt == GIT_TRIES or not verdict:
                raise
            log(f"WARN transient relay failure (attempt {attempt}/{GIT_TRIES}), "
                f"retrying in {GIT_RETRY_DELAY}s: {str(e)[:200]}")
            time.sleep(GIT_RETRY_DELAY)


def post(text):
    """Channel post. Best-effort: a failure here must not abort the tick, or a
    Buzz outage would stop mirroring as well as reporting."""
    try:
        buzz_cli("messages", "send", "--channel", CHANNEL, "--content", text)
    except Exception as e:
        log(f"WARN could not post to buzz: {e}")


def open_pr(buzz_owner, repo_id, branch, sha, gh_repo):
    try:
        out = buzz_cli(
            "pr", "open",
            "--repo-owner", buzz_owner,
            "--repo-id", repo_id,
            "--subject", f"GitHub is ahead of Buzz main ({sha[:7]})",
            "--commit", sha,
            "--clone", buzz_url(buzz_owner, repo_id),
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


def propose_github_ahead(d, buzz_owner, repo_id, gh_repo, gh_tip, state):
    """The fallback for a repo whose Buzz `main` this bot may not push.

    Puts GitHub's tip on Buzz as a branch, opens a PR for it, and halts with the
    one-line command a human runs to adopt it. Sticky on the proposed sha, so a
    repo that stays ahead is one message rather than one per tick.

    This was the only GitHub-ahead behaviour until `push:member` made adopting
    it possible. It stays because protection is per-repo: a repo that should
    never be written from GitHub keeps `push:owner` and lands here.
    """
    branch = f"mirror/github-ahead-{gh_tip[:7]}"
    if state.get(repo_id, {}).get("proposed") == gh_tip:
        return
    git(d, "push", buzz_url(buzz_owner, repo_id), f"{gh_tip}:refs/heads/{branch}")
    link = open_pr(buzz_owner, repo_id, branch, gh_tip, gh_repo)
    state[repo_id] = {"halted": "github-ahead", "proposed": gh_tip,
                      "since": int(time.time())}
    save_state(state)
    log(f"HALT {repo_id}: github-ahead at {gh_tip[:7]}")
    post(
        f"**GitHub is ahead of Buzz on `{repo_id}`, and this bot cannot push "
        f"Buzz main here.**\n\n"
        f"Pushed `{branch}` to Buzz. Fast-forward main to adopt it:\n\n"
        f"```sh\ngit push buzz {gh_tip}:refs/heads/main\n```\n\n"
        + (f"{link}\n\n" if link else "")
        + "Deploys for this repo are frozen until this is resolved."
    )


def reconcile(buzz_owner, repo_id, gh_repo, token, state):
    d = ensure_repo(repo_id)
    (buzz_tip, _), (gh_tip, gh_heads) = fetch_both(d, buzz_owner, repo_id, gh_repo, token)

    if gh_tip is None and gh_heads:
        # GitHub has branches but none of them is main: a repo whose default
        # branch is named something else. Creating a main from Buzz next to
        # that history would be a guess about intent; the fix is a rename on
        # GitHub, and the next tick picks it up.
        halt(state, repo_id, "github-no-main",
             f"`{gh_repo}` has branches but no `main`, and this mirror is "
             f"main-only. Rename the default branch to `main` (or push one) "
             f"to start mirroring.")
        return

    if buzz_tip is None and gh_tip is None:
        # Announced before either side's first push. Nothing to reconcile yet,
        # and not an error: the pair starts mirroring at the first commit.
        clear_halt(state, repo_id)
        return

    if buzz_tip is None:
        # A fresh Buzz repo paired with existing GitHub history - the
        # onboarding shape. Creating main IS the adoption; there is no local
        # history for ancestry to protect. Other branches on the Buzz side
        # (an earlier tick's proposal branch, say) change nothing: main is
        # still absent.
        try:
            git(d, "push", buzz_url(buzz_owner, repo_id), f"{gh_tip}:refs/heads/main")
        except RuntimeError as e:
            log(f"{repo_id}: buzz main refused the bootstrap ({e}); proposing instead")
            propose_github_ahead(d, buzz_owner, repo_id, gh_repo, gh_tip, state)
            return
        log(f"{repo_id}: buzz main created at {gh_tip[:7]} (bootstrapped from github)")
        post(f"`{repo_id}`: empty Buzz repo - created main at `{gh_tip[:7]}` from "
             f"GitHub (`{gh_repo}`). Nothing was rewritten.")
        clear_halt(state, repo_id)
        return

    if gh_tip is None:
        # A truly empty GitHub repo paired with Buzz history: seed it. Same
        # plain non-force push as the steady-state direction, and silent for
        # the same reason that direction is.
        git(d, "push", github_url(gh_repo, token), f"{buzz_tip}:refs/heads/main")
        log(f"{repo_id}: github main created at {buzz_tip[:7]} (seeded from buzz)")
        clear_halt(state, repo_id)
        return

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
        # GitHub ahead by a clean fast-forward: adopt it.
        try:
            git(d, "push", buzz_url(buzz_owner, repo_id), f"{gh_tip}:refs/heads/main")
        except RuntimeError as e:
            # Almost always the relay refusing the push because this repo still
            # has `push:owner` on main. Deliberately not matched on the denial
            # text: a transient network failure lands here too, and the propose
            # path's own push fails the same way, which halts as a reconcile
            # failure. Guessing which one it was would only add a way to guess
            # wrong.
            log(f"{repo_id}: buzz main refused the fast-forward ({e}); proposing instead")
            propose_github_ahead(d, buzz_owner, repo_id, gh_repo, gh_tip, state)
            return
        log(f"{repo_id}: buzz {buzz_tip[:7]} -> {gh_tip[:7]} (adopted from github)")
        post(f"`{repo_id}`: Buzz main fast-forwarded to `{gh_tip[:7]}` from GitHub "
             f"(`{gh_repo}`). Nothing was rewritten.")
        clear_halt(state, repo_id)
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
    pairs, ok = discover()
    if not pairs:
        # Never intended in a working deployment, and silence would read exactly
        # like "everything is in sync". Loud on purpose.
        log("ERROR no repos to mirror - check the App's grants and the `web` tags")
        return False
    for repo_id, buzz_owner, gh_repo, token in pairs:
        try:
            reconcile(buzz_owner, repo_id, gh_repo, token, state)
        except Exception as e:
            ok = False
            # Name the subsystem. "Looks like a GitHub outage but is actually a
            # revoked Buzz key" was the failure mode worth spending a branch on.
            # Unreachable and denied are also kept apart: halts are sticky by
            # reason, so a real auth failure arriving while a 5xx halt is live
            # must post as its own message, not be swallowed by it.
            msg = str(e)
            # cli_retryable returns None on every path that reaches here today:
            # the only buzz_cli callers left are post() and open_pr(), and both
            # swallow their own exceptions, so a buzz-CLI error never surfaces
            # in this handler. Kept because that is a property of the current
            # call graph rather than a rule, and the day a CLI error does reach
            # here the flag is the better answer. Untested on purpose - there is
            # no way to exercise it without inventing a caller.
            verdict = cli_retryable(e)
            transient = bool(TRANSIENT.search(msg)) if verdict is None else verdict
            if "buzz.gitcoin.co" in msg or "not a relay member" in msg:
                reason = "buzz-unavailable" if transient else "buzz-auth-failed"
            elif "github.com" in msg or "api.github.com" in msg:
                reason = "github-unavailable" if transient else "github-auth-failed"
            else:
                reason = "reconcile-failed"
            detail = f"```\n{msg[:800]}\n```"
            # Gated on the reason, not on the wording alone. GitHub renders an
            # installation that lacks access as `remote: Repository not found.`
            # too, and the note's first line ("the label above says auth because
            # the URL is a Buzz one") is false there - it would send the reader
            # to a clone tag, a channel member list and the relay's event
            # ordering for a missing App grant.
            if REPO_NOT_FOUND.search(msg):
                if reason.startswith("buzz-"):
                    detail += AMBIGUOUS_404
                elif reason.startswith("github-"):
                    detail += GITHUB_404
            halt(state, repo_id, reason, detail)
    if ok:
        touch_last_success()
    return ok


def hold_lock():
    """Take the run lock, or return None if another run already has it.

    systemd already refuses to start a second instance of an active unit, so the
    timed runs cannot overlap on their own. This covers what it does not: a
    hand-run `docker run` alongside a timed one. Two runs sharing the bare
    clones and state.json would interleave fetches and let the last writer
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
               The deployed shape: one container per run, started by a systemd
               timer, where the *failure itself* is the alert via OnFailure=.

      (default) loop forever. For when a scheduler is not available, or when a
               sub-minute interval is wanted. Liveness then needs the
               healthcheck + a separate staleness check, because a wedged
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
            # Discovery itself can fail - GitHub down, token revoked - and that
            # is not a per-repo halt. Exit non-zero so the unit reports it.
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
