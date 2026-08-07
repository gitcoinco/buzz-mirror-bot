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

Halt reasons are named — `buzz-auth-failed`, `github-auth-failed`, `github-not-installed`,
`diverged`, `push-rejected`, `reconcile-failed`. The sharpest failure mode here is mirror-bot
being dropped from a bound channel: git read on Buzz is membership-gated, so it presents as a 404
on fetch and looks exactly like a GitHub outage. Naming the subsystem is the entire fix.

Halt messages also say **"deploys for this repo are frozen"**, because Coolify deploys from
GitHub. A halted mirror is not cosmetic — Buzz-side work stops reaching production.

## Run it as a scheduled task, not a long-running process

Every tick is self-contained: state lives in `state.json` and the bare clones on the volume,
never in memory. So the loop is *only* a scheduler, and if the platform already has one it should
own the schedule instead.

```sh
python3 mirror/daemon.py --once     # one reconcile, exit non-zero if anything failed
```

Run that as a **Coolify scheduled task**. The run's own exit status is the alert, and Coolify's
**Scheduled Task Failure** notification fires on it directly — email / Slack / Discord / Telegram
/ Pushover / webhook, out-of-band from Buzz and from the daemon's own identity.

That deletes a whole layer: no `last-success` file, no staleness probe, no container healthcheck,
no second scheduled task watching the first. One thing runs, and when it fails you are told.

### Liveness

Falls out of the above. A failed run notifies. A run that never happens is Coolify's scheduler
being down, which is the same failure as the host being down.

The **loop mode** (no `--once`) exists for when no scheduler is available or a sub-minute interval
is wanted. It costs more: `last-success` + `mirror/healthcheck.sh` as a container `HEALTHCHECK`
so Coolify restarts a wedged-but-running process, plus a separate scheduled staleness check for
the alert. A wedged long-running process never exits to be noticed, and an alert that shares a
process with the thing it watches is not an alert.

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
| `GITHUB_APP_PEM_B64` | the App private key, base64 (`base64 < key.pem \| tr -d '\\n'`) |
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

Use **`GITHUB_APP_PEM_B64`**. A PEM is multi-line and secret-store form fields are not, so a raw
paste is the most likely setup mistake there is. `GITHUB_APP_PEM` is also accepted for a
single-line value with literal `\n` escapes; both paths normalise to the same bytes. The
entrypoint checks for `BEGIN`/`END` markers and refuses to start on a mangled value rather than
failing later with an opaque JWT error.

### Creating the GitHub App

There is no API for this — the App is created in a browser, once. Create it **under an
organisation**, not a personal account, so it outlives any one person:
`https://github.com/organizations/<org>/settings/apps/new`.

Where it is *created* and where it can be *installed* are independent: an org-owned App set to
*Any account* installs onto personal accounts and other orgs perfectly well. Creating it in an org
is purely about ownership and survivorship.

The form is long and almost all of it is irrelevant. What matters:

| field | value | why |
|---|---|---|
| GitHub App name | anything unique across GitHub | e.g. `<org>-buzz-mirror` |
| Homepage URL | any URL | required by the form; nothing reads it |
| Callback URL / Setup URL | blank | no user-facing OAuth flow |
| Request user authorization (OAuth) | **unchecked** | the App acts as itself, never on behalf of a user |
| Expire user authorization tokens | leave **checked** (default) | inert here — it governs *user*-to-server tokens, and with OAuth off none are ever minted. Leave it on anyway: unchecking means non-expiring user tokens if anyone enables OAuth later. It does **not** affect the installation tokens the daemon uses, which expire after an hour regardless and are not configurable |
| Enable Device Flow | **unchecked** | — |
| **Webhook → Active** | **UNCHECK** | default is *on* and then demands a public Webhook URL. The daemon polls, so it needs no ingress at all |
| Subscribe to events | none | greyed out once the webhook is inactive |
| Where can this be installed | **Any account** | see below — required if the repos you mirror span more than one account |

Repository permissions — set exactly two, leave every other row on *No access*:

- **Contents: Read and write**
- **Workflows: Read and write**

`Metadata: Read-only` selects itself and cannot be turned off; that is expected. Set no
organisation or account permissions.

`workflows: write` is not optional: the mirrored repos carry files under `.github/workflows/`,
and GitHub rejects any App push that touches them without it. Note what it grants — an actor who
can write a workflow file can execute arbitrary code in Actions with that repo's secrets. It is
the sharpest permission in this design and it is load-bearing.

After **Create GitHub App**:

1. The **App ID** is at the top of the settings page → `GITHUB_APP_ID`.
2. **Private keys → Generate a private key** downloads a `.pem`. Keys are generated, not shown
   again — but you can generate more and revoke old ones at any time, so losing it is recoverable.
3. **Install App** (left sidebar) → **Only select repositories** → pick the repos to mirror.
   Repeat **once per account** whose repos you mirror. This is the per-repo approval step, and
   installing on a repo is the only thing needed to add it later.

### Installations are per account, not per App

An App set to *Any account* gets a **separate installation for every account it is installed on**,
each with its own id and its own tokens. A token minted for one installation is not valid for
another, so the unit of authentication is the *account*, not the App. The daemon handles this:
it reads the owner from each `MIRROR_REPOS` entry and mints one token per distinct account.

Two consequences worth knowing:

- **The App must be installed separately on every account you mirror** — your personal account and
  each org are separate installs. Forgetting one halts only that repo, with reason
  `github-not-installed` naming the account; the others keep mirroring.
- **`Any account` makes the App's install page publicly reachable** at `github.com/apps/<slug>`,
  and GitHub offers no way to restrict it to a list of accounts. If a stranger installs it they
  are granting *this daemon* access to *their* repos, not gaining access to yours — so the
  exposure is theirs, not yours. The daemon ignores any installation whose account is not named
  in `MIRROR_REPOS`.

Choose *Only on this account* only if every repo you will ever mirror lives in that one account.

### Where the private key goes

Into the Coolify application as `GITHUB_APP_PEM_B64`, and nowhere else. Not the repo, not a
checkout, not a shared drive.

```sh
base64 < ~/Downloads/<app-name>.<date>.private-key.pem | tr -d '\n' | pbcopy
```

(`tr -d '\n'` rather than `base64 -w0`: the flag is GNU-only and macOS ships BSD base64.)

Then delete the download. The key is regenerable, so a local copy is pure liability:

```sh
rm -P ~/Downloads/<app-name>.<date>.private-key.pem   # -P overwrites first; macOS
```

## Tests

```sh
python3 mirror/test_reconcile.py
```

Drives all four ancestry cases, plus stickiness and recovery, through `reconcile()` against real
bare repos — not mocks. The expensive bug in a mirror is pushing the wrong direction, so that path
is tested for real.
