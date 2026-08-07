# The mirror daemon

The Action in this repo works, but it costs a workflow file and two Actions secrets **in every
mirrored repo**. That per-repo cost is the whole problem: it does not scale past a handful of
repos, and it means adding a repo to the mirror is a commit plus a secrets dance rather than a
config line.

The daemon replaces it. One long-running container mirrors any number of repos, and adding one is:

1. a line in `MIRROR_REPOS`
2. installing the GitHub App on that repo

Nothing lands in the mirrored repo itself.

## What it does

Every 60s, per repo, it fetches both `main`s into a bare clone and compares them:

| ancestry | action |
|---|---|
| tips equal | nothing; clear any halt |
| GitHub is an ancestor of Buzz | fast-forward GitHub to Buzz's tip |
| Buzz is an ancestor of GitHub | **propose only** — see below |
| neither | halt as `diverged`; a human reconciles |

Every push is plain and non-force. Nothing here can rewrite history on either side; the worst
outcome is that a repo stops mirroring and says so.

### GitHub ahead is the error case, and it is propose-only

The daemon never pushes GitHub's tip onto Buzz `main`. It pushes it to
`mirror/github-ahead-<sha>`, opens a NIP-34 PR, and posts the one-line command to adopt it:

```sh
git push buzz <github-sha>:refs/heads/main
```

Two reasons, and both matter:

- **It would invert the trust direction.** Buzz is origin. Auto-pushing GitHub's tip onto Buzz
  `main` would make anyone with GitHub write into someone who can write Buzz `main`.
- **It would not work anyway.** The relay resolves push role from repo ownership
  (`is_agent_owner`) and channel role — a NIP-OA auth tag grants *membership*, not push rights,
  and a channel `Bot` role is coerced to `Member` for git. So mirror-bot cannot push protected
  `main`. It *can* create any unprotected ref, which is exactly what the proposal branch needs.

There is no server-side merge on Buzz. `buzz pr status --status merged --merge-commit <sha>`
takes the merge commit as an *input* — setting a PR merged records that you merged it. Since
GitHub-ahead means Buzz `main` is an ancestor, adopting it is a fast-forward, so the one-line
command above is the whole operation.

### Halts are sticky, and they say what they cost

One message per halt, not one per tick. The halt holds until the tips converge, then the daemon
posts a recovery message.

Halt reasons are named — `buzz-auth-failed`, `github-auth-failed`, `diverged`, `push-rejected`,
`reconcile-failed` — because the sharpest failure mode here is mirror-bot being dropped from a
bound channel: git read on Buzz is membership-gated, so it presents as a 404 on fetch and looks
exactly like a GitHub outage. Naming the subsystem is the entire fix.

Halt messages also say **"deploys for this repo are frozen"**, because Coolify deploys from
GitHub. A halted mirror is not cosmetic — Buzz-side work stops reaching production.

## Liveness

The daemon writes `last-success` after every fully successful tick. `mirror/healthcheck.sh` reads
it and exits non-zero when it is stale. That one script is used twice:

1. **Container `HEALTHCHECK`** → Coolify restarts a wedged-but-running daemon. This is the case a
   restart policy alone misses: a daemon stuck in a halt or failing to authenticate never crashes.
2. **Coolify scheduled task** → Coolify fires a **Scheduled Task Failure** notification.

(2) is the one that matters, because it is the only alert that does not share a failure domain
with the daemon. An alert posted by the daemon over Buzz dies at exactly the moment its Buzz
identity is revoked — which is the wedge it most needs to report.

> **Verify the notification fires.** There are open Coolify reports of failure notifications not
> being sent while success notifications are. Test it once with a deliberate failure. An alarm you
> believe in but do not have is worse than no alarm.

**Not covered:** whole-host death, since Coolify dies with it. Accepted — it would be evident
from everything else being down.

## Why polling, not the ref-state subscription

The relay emits a `kind:30618` ref-state event on every successful push, and subscribing would cut
latency from ~60s to ~2s. It would also add a persistent websocket, NIP-42 AUTH, reconnect
handling, and an input that is **client-publishable** — a member can publish a `kind:30618` with
any `d` tag, and it coexists with the relay's rather than replacing it. Any subscriber must pin
`authors` to the relay key *and* treat the event as a trigger only, deriving real tips from an
authenticated fetch.

That is a lot of surface for latency on a mirror whose consumer redeploys on its own schedule.
The reconcile loop is the correctness mechanism either way, so a subscription can be added later
as a pure latency optimisation without touching the decision tree.

## Deploying

Create it as a Coolify application from `docker-compose.yml`, then `tofu import` it when the
OpenTofu migration reaches it — the `coolify-terraform/coolify` provider supports import on every
stateful resource, so building it in the UI now costs no rework later.

### Configuration

| var | what |
|---|---|
| `BUZZ_PRIVATE_KEY` | mirror-bot's nostr key. Must be a member of every bound channel |
| `BUZZ_AUTH_TAG` | NIP-OA owner attestation |
| `GITHUB_APP_ID` | the App's id |
| `GITHUB_APP_PEM` | the App private key |
| `BUZZ_REPO_OWNER` | 64-char hex pubkey that announced the repos |
| `MIRROR_REPOS` | JSON, `{"<buzz-repo-id>": "<owner>/<repo>"}` |
| `MIRROR_ALERT_CHANNEL` | channel UUID for halt and recovery messages |
| `MIRROR_INTERVAL_SECS` | reconcile interval, default 60 |

The **installation id is not configured** — the daemon discovers it from the PEM via
`GET /app/installations`. It is derivable, so it should not be one more value a human has to find.

The PEM arrives as an environment variable and the entrypoint moves it to a `0400` tmpfs file
before starting the daemon. That is weaker than systemd's `LoadCredential`, since it transits an
env var on the way in; it is the accepted cost of deploying through Coolify rather than as a host
unit.

### GitHub App permissions

`contents: write` **and** `workflows: write`.

`workflows: write` is not optional: both mirrored repos carry files under `.github/workflows/`,
and GitHub rejects any App push that touches them without it. Note what it grants — an actor who
can write a workflow file can execute arbitrary code in Actions with that repo's secrets.

## Tests

```sh
python3 mirror/test_reconcile.py
```

Drives all four ancestry cases, plus stickiness and recovery, through `reconcile()` against real
bare repos — not mocks. The expensive bug in a mirror is pushing the wrong direction, so that path
is tested for real.
