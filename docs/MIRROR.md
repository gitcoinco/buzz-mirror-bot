# The mirror

The Action in this repo works, but it costs a workflow file and two Actions secrets **in every
mirrored repo**. That per-repo cost is the whole problem: it does not scale past a handful of
repos, and it means adding a repo to the mirror is a commit plus a secrets dance rather than a
config line.

This replaces it. One deployment mirrors any number of repos, and enrolling one is **a tick in
the GitHub App's install UI**. There is no repo list anywhere — see [What gets
mirrored](#what-gets-mirrored). Nothing lands in the mirrored repo itself, and nothing needs a
config edit or a redeploy.

## What it does

Per repo, per run, it fetches both `main`s into a bare clone and compares them:

| ancestry | action |
|---|---|
| tips equal | nothing; clear any halt |
| GitHub is an ancestor of Buzz | fast-forward GitHub to Buzz's tip |
| Buzz is an ancestor of GitHub | **propose only** — see below |
| neither | halt as `diverged`; a human reconciles |

Every push is plain and non-force. Nothing here can rewrite history on either side; the worst
outcome is that a repo stops mirroring and says so.

### GitHub ahead is the error case, and it is propose-only

The mirror never pushes GitHub's tip onto Buzz `main`. It pushes it to
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

One message per halt, not one per tick. The halt holds until the tips converge, then it
posts a recovery message.

Halt reasons are named — `buzz-auth-failed`, `github-auth-failed`,
`diverged`, `push-rejected`, `reconcile-failed`. The sharpest failure mode here is mirror-bot
being dropped from a bound channel: git read on Buzz is membership-gated, so it presents as a 404
on fetch and looks exactly like a GitHub outage. Naming the subsystem is the entire fix.

Halt messages also say **"deploys for this repo are frozen"**, because Coolify deploys from
GitHub. A halted mirror is not cosmetic — Buzz-side work stops reaching production.

## What gets mirrored

Discovered every run, never configured. The mirror set is the **intersection** of two opt-ins:

1. the repo is **announced on Buzz** under `BUZZ_REPO_OWNER` with a GitHub URL in its `web` tag
2. the repo is **granted to the GitHub App** — install with *Only select repositories* and tick it

Enrolling a repo is (2). Unenrolling is unticking it. No env var, no redeploy.

The pairing comes from the announcement's `web` tag rather than from matching names, because the
names do not match: `regenos-dev` is `irlfund/regenOS` and `local-almanac-mirror` is
`irlfund/local-almanac`. The `kind:30617` already carries both `d` (the repo id the git endpoint
is keyed by) and `web`, so the mapping is *declared* by whoever created the repo.

Requiring both sides is also what makes an **"Any account"** App safe to leave open: a stranger
who installs it gets nothing mirrored, because none of their repos are announced under your
pubkey. See [Multi-account installations](#multi-account-installations).

A repo that satisfies only one side is **logged, not halted** — the GitHub grant is the enrolment
action, so a Buzz repo nobody intends to mirror is not an error, and halting on it would alert
forever. Both directions appear in the log every run, so a mis-click is visible:

```
github: irlfund granted 3 repo(s): agentic-engineering-infra, local-almanac, regenOS
mirroring regenos-dev -> irlfund/regenOS
skipped, announced on buzz but the App is not granted them: buzz-only -> irlfund/never-granted
skipped, granted to the App but not announced on buzz: gitcoinco/some-org-repo
```

Discovering **zero** repos exits non-zero. An empty mirror set is never intended, and staying
quiet about it would read exactly like "everything is in sync".

`MIRROR_ONLY` (a JSON array of buzz repo-ids) restricts the set further. It exists to point a
test deployment at one scratch repo; leave it unset in production.

## Run it as a scheduled task, not a long-running process

Every run is self-contained: state lives in `state.json` and the bare clones on the volume, never
in memory. So the loop is *only* a scheduler, and if the platform already has one it should own
the schedule instead.

```sh
python3 mirror/sync.py --once     # one reconcile, exit non-zero if anything failed
```

Run that as a **Coolify scheduled task**. The run's own exit status is the alert, and Coolify's
**Scheduled Task Failure** notification fires on it directly — email / Slack / Discord / Telegram
/ Pushover / webhook, out-of-band from Buzz and from the mirror's own identity.

### It still needs a container to be up

Coolify's scheduled tasks `docker exec` into an **already-running** container — from
`app/Jobs/ScheduledTaskJob.php`:

```php
$cmd  = "sh -c '".str_replace("'", "'\''", $this->task->command)."'";
$exec = "docker exec {$containerName} {$cmd}";
```

They do not start one, and Coolify has no host-level cron at all
([#8500](https://github.com/coollabsio/coolify/issues/8500)). So the deployed shape is:

| | |
|---|---|
| main process | `sleep infinity` — exists only to be exec'd into |
| scheduled task | `/app/mirror/entrypoint.sh --once` — the actual mirror run |

A `sleep infinity` main process looks pointless. The payoff is that **one notification path covers
two failures**: the command runs through `instant_remote_process(..., throwError: true)`, so a
non-zero exit is recorded as `status => 'failed'` and sends `TaskFailed` — and if the container is
gone, container discovery finds no name and the exec fails the same way. A dead mirror and a
missing mirror alert identically.

That still deletes a layer: no `last-success` file, no staleness probe, no container healthcheck,
no second scheduled task watching the first.

Concurrent runs are handled with an exclusive `flock` on `$MIRROR_STATE_DIR/lock`, because Coolify
does not dedupe: if a reconcile outlives the cron interval the next exec starts anyway, and two
runs would interleave fetches on the same bare clones. An overlapping run exits **0** — paging
someone about a slow tick is how an alert channel gets ignored.

### Liveness

Falls out of the above. A failed run notifies, and so does a missing container. A run that never
happens is Coolify's scheduler being down, which is the same failure as the host being down.

The **loop mode** (no `--once`) exists for when no scheduler is available or a sub-minute interval
is wanted. It costs more: drop `command:` from the compose file, and add back `last-success` +
`mirror/healthcheck.sh` as a container `HEALTHCHECK` so Coolify restarts a wedged-but-running
process, plus a separate scheduled staleness check for the alert:

```dockerfile
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=2 \
    CMD /app/mirror/healthcheck.sh
```

A wedged long-running process never exits to be noticed, and an alert that shares a process with
the thing it watches is not an alert.

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

The Coolify application does not exist until you create it. `docker-compose.yml` is its
definition: Coolify treats a compose file as [the single source of
truth](https://coolify.io/docs/knowledge-base/docker/compose) — it parses every `${VAR}` into a UI
field, and the `${VAR:?}` ones block deploy until filled. The file is the form.

**Source must be GitHub.** Coolify cannot clone from Buzz: Buzz git authenticates with NIP-98 via
`git-credential-nostr` and Coolify has no such helper. This is the same reason GitHub `main` sits
upstream of production. So whatever you deploy has to reach GitHub `main` first.

**+ New → Public Repository**

| field | value |
|---|---|
| Repository URL | `https://github.com/gitcoinco/buzz-mirror-bot` |
| Branch | `main` |
| Build Pack | Docker Compose |
| Compose file location | `/docker-compose.yml` |

Fill the four required environment variables — `BUZZ_PRIVATE_KEY`, `BUZZ_AUTH_TAG`,
`GITHUB_APP_ID`, `GITHUB_APP_PEM_B64` — and deploy. The container comes up running
`sleep infinity` and does nothing; that is correct, it is only an exec target.

Then **Scheduled Tasks → + Add**:

| field | value |
|---|---|
| Name | `mirror` |
| Command | `/app/mirror/entrypoint.sh --once` |
| Frequency | `*/5 * * * *` |

If it asks for a container name, that is the compose service name: `mirror`.

Finally, **Team → Notifications → Scheduled Task Failure** must be enabled on a channel someone
reads. Without it the alerting design here is inert.

`tofu import` it when the OpenTofu migration reaches it — the `coolify-terraform/coolify` provider
supports import on every stateful resource, so building it in the UI now costs no rework later.

### Do not let the mirror mirror itself

Coolify deploys this repo from GitHub, and GitHub would be fed by the mirror. If the mirror breaks,
a fix pushed to Buzz cannot reach GitHub, so it cannot deploy, so it stays broken.

**Keep `.github/workflows/buzz-mirror-main.yml` on `buzz-mirror-bot` specifically.** Everywhere
else it is exactly the per-repo cost this replaces and should be deleted once the mirror is proven.
Here it is not redundancy — it is the only thing that breaks the deadlock.

### Configuration

| var | what |
|---|---|
| `BUZZ_PRIVATE_KEY` | mirror-bot's nostr key. Must be a member of every bound channel |
| `BUZZ_AUTH_TAG` | NIP-OA owner attestation |
| `GITHUB_APP_ID` | the App's id |
| `GITHUB_APP_PEM_B64` | the App private key, base64 (`base64 < key.pem \| tr -d '\\n'`) |
| `BUZZ_REPO_OWNER` | 64-char hex pubkey that announced the repos |
| `MIRROR_ALERT_CHANNEL` | channel UUID for halt and recovery messages |
| `MIRROR_ONLY` | *optional.* JSON array of buzz repo-ids to restrict to. Unset = everything discovered |
| `MIRROR_INTERVAL_SECS` | loop mode only; ignored by `--once`. Default 60 |

**There is no repo list.** `MIRROR_REPOS` is gone; setting it is now a hard startup error rather
than a silently ignored value. See [What gets mirrored](#what-gets-mirrored).

The **installation id is not configured** either — it is discovered from the PEM via
`GET /app/installations`. It is derivable, so it should not be one more value a human has to find.

The PEM arrives as an environment variable and the entrypoint moves it to a `0400` tmpfs file
before anything else runs. That is weaker than systemd's `LoadCredential`, since it transits an
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
| Expire user authorization tokens | leave **checked** (default) | inert here — it governs *user*-to-server tokens, and with OAuth off none are ever minted. Leave it on anyway: unchecking means non-expiring user tokens if anyone enables OAuth later. It does **not** affect the installation tokens the mirror uses, which expire after an hour regardless and are not configurable |
| Enable Device Flow | **unchecked** | — |
| **Webhook → Active** | **UNCHECK** | default is *on* and then demands a public Webhook URL. It polls, so it needs no ingress at all |
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

### Multi-account installations

An App set to *Any account* gets a **separate installation for every account it is installed on**,
each with its own id and its own tokens. A token minted for one installation is not valid for
another, so the unit of authentication is the *account*, not the App. Discovery walks every
installation and pairs each repo with the token for *its own* account.

Three consequences worth knowing:

- **The App must be installed separately on every account you mirror** — your personal account and
  each org are separate installs. Forgetting one just means those repos never appear in the
  discovered set; the log line naming what each account granted is where you see it.
- **`Any account` makes the App's install page publicly reachable** at `github.com/apps/<slug>`,
  and GitHub offers no way to restrict it to a list of accounts. If a stranger installs it they
  are granting *this App* access to *their* repos, not gaining access to yours — the exposure is
  theirs. And because the mirror set requires a Buzz announcement under your pubkey too, their
  repos are skipped rather than mirrored anywhere.
- **One token is minted per installation, including ones nothing is used from.** There is no
  App-JWT endpoint that lists an installation's repositories, so the token is the only way to see
  inside one; unwanted ones are discarded immediately. Every account's grants are logged, so a
  stray installation is visible rather than merely harmless.

What bounds the blast radius is the **per-install repo selection**: choose *Only select
repositories*, never *All repositories*. A compromise of the PEM then reaches exactly the repos
that were ticked, per account, and nothing else.

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
