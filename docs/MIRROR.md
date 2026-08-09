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
| Buzz is an ancestor of GitHub | fast-forward Buzz to GitHub's tip — see below |
| neither | halt as `diverged`; a human reconciles |

Every push is plain and non-force. Nothing here can rewrite history on either side; the worst
outcome is that a repo stops mirroring and says so.

### GitHub ahead is an update, not an error

A strict ancestor is a fast-forward in either direction, and adopting one needs no judgement.
So the mirror adopts it and posts a line saying it did:

> `agentic-engineering-infra`: Buzz main fast-forwarded to `12082eb` from GitHub
> (`irlfund/agentic-engineering-infra`). Nothing was rewritten.

This is a change from the original design, which proposed a branch and halted. The reason for
the change is that halting on GitHub-ahead assumes **every** mirrored repo is developed
Buzz-first. That is the intended habit, but it is not universally true — the infra repo is
worked on GitHub-first on purpose — and such a repo would sit in a permanent halt that means
nothing except "someone worked in the usual place for that repo". An alarm that fires forever
is not an alarm. Real divergence still halts, and that is what the alarm was for.

**What it costs.** GitHub write now reaches Buzz `main` with no review step. That set already
reaches production directly — Coolify deploys from GitHub — so this grants it nothing it did not
have. What is genuinely lost is the *nudge* toward working Buzz-first; the channel post is the
replacement, visible rather than blocking.

**What makes it possible.** `push:member` on `refs/heads/main` in the repo's `buzz-protect`
tags. The relay takes `max(explicit push:role, default_min_role(ref, kind))`, and its built-in
defaults on a branch are Member for a fast-forward, Admin for a non-fast-forward or a delete —
and an explicit rule can never *weaken* the destructive two
(`buzz-core/src/git_perms.rs`, `evaluate_ref_update` / `default_min_role`). So that one tag means
exactly "mirror-bot may fast-forward `main` and nothing else". Ownership still matters for
everything else: a NIP-OA auth tag grants *membership*, not push rights, and a channel `Bot`
role is coerced to `Member` for git.

### Where `main` is not writable, it still proposes

Protection is per-repo, so this needed no flag day and a repo that should never be written from
GitHub simply keeps `push:owner`. When the relay refuses the push, the mirror falls back to the
original behaviour: it pushes GitHub's tip to `mirror/github-ahead-<sha>`, opens a NIP-34 PR,
halts, and posts the one-line command to adopt it:

```sh
git push buzz <github-sha>:refs/heads/main
```

The fallback is not matched on the relay's denial text — a transient network failure lands there
too, and the proposal's own push fails the same way, which halts as a reconcile failure.
Guessing which one it was would only add a way to guess wrong.

**As of 2026-08-09 no repo uses this path.** All four carry
`push:member no-force-push no-delete` on `refs/heads/main`:

| repo | | |
|---|---|---|
| `regenos-dev` | `push:member` | app repo |
| `local-almanac-mirror` | `push:member` | app repo |
| `agentic-engineering-infra` | `push:member` | control plane — see the note below |
| `buzz-mirror-bot` | `push:member` | not in the mirror set anyway (`GITHUB_OWNER`) |

That is a deliberate call by the owner (2026-08-09): agentic upgrade of the fleet is worth more
right now than the review gate, and the flip back is one republished announcement per repo.

For **aei** it is worth being precise about what that buys and costs, because aei's build is a
converge that ends in `tofu apply` as root. The gate it removes is *not* the one it looks like:
the converge is triggered by a **GitHub** push, so a direct push to GitHub always reached `tofu
apply` without passing through Buzz at all. What `push:owner` was actually gating is the other
direction — Buzz → GitHub. With `push:member`, any Member of aei's bound channel can fast-forward
Buzz `main`, the mirror carries it to GitHub, and the converge runs it as root. So the set that
can reach root on infra-box widens from "GitHub write + repo owner" to "GitHub write + repo owner
+ aei channel Members". That widening is the feature being bought, not a side effect: it is what
lets an agent ship an infra change end to end.

The claim in aei's `wiki/architecture/fleet-pipeline.md` that repo-driven root execution is
"acceptable ONLY because aei is merge-gated and `push:owner` in the mirror" no longer holds as
written and needs updating there.

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

Two Buzz repos naming the **same** GitHub repo in their `web` tags is the one pairing problem that
does fail the run. They would take turns fast-forwarding one GitHub `main` from unrelated
histories, and there is no safe guess about which is authoritative, so both are dropped:

```
ERROR irlfund/regenos is claimed by 2 buzz repos (regenos-dev, regenos-dupe) - skipping all of
them; fix the `web` tags so each points at its own repo
```

Everything else still mirrors — one bad `web` tag should not stop the rest.

**There is no way to restrict the set further, on purpose.** Enrolment is the App grant and
nothing else: granted means fully enrolled, in both directions — the mirror pushes it and the
dispatcher builds it, because those are two halves of one system rather than two things to opt
into separately. `MIRROR_ONLY` used to exist as a test escape hatch and setting it is now a hard
startup error. To mirror one scratch repo, grant the App one scratch repo.

## One container per run, on a systemd timer

Every run is self-contained: state lives in `state.json` and the bare clones on the volume, never
in memory. So nothing needs a process to stay alive.

```sh
python3 mirror/sync.py --once     # one reconcile, exit non-zero if anything failed
```

The deployed shape runs that in a fresh container every five minutes, from a systemd timer on
**infra-box** (`deploy/`):

| unit | what |
|---|---|
| `buzz-mirror.timer` | `OnCalendar=*:0/5`, `Persistent=true` |
| `buzz-mirror.service` | `Type=oneshot`, runs `deploy/run-once.sh` |
| `buzz-mirror-alert@.service` | `OnFailure=` target — posts the failure to Buzz |

`run-once.sh` asks the local issuer for an installation token and does
`docker run --rm <image> --once`. Nothing is kept up between runs.

### Why not a Coolify scheduled task

That was the earlier design and it cost two things. Coolify's scheduled tasks `docker exec` into
an **already-running** container (`app/Jobs/ScheduledTaskJob.php`) rather than starting one, and
Coolify has no host-level cron ([#8500](https://github.com/coollabsio/coolify/issues/8500)) — so
the container had to run `sleep infinity` purely to be an exec target. And Coolify keeps every app
env var in its database, which is where the GitHub App PEM would have lived.

Running on infra-box removes both. The issuer is local, so the mirror is handed a token that
expires in an hour instead of a key that never does, and there is no exec target because each run
is its own container.

Concurrent runs: systemd will not start a second instance of an active unit, so the timed runs
cannot overlap. `sync.py` keeps its exclusive `flock` on `$MIRROR_STATE_DIR/lock` anyway, for the
case systemd cannot see — a hand-run `docker run` alongside a timed one. An overlapping run exits
**0**; paging someone about a slow tick is how an alert channel gets ignored.

### Liveness

Two failures, two mechanisms, because one cannot cover both:

- **A run that fails** → the unit's `OnFailure=` fires `buzz-mirror-alert@.service`, which posts
  the unit name and the last 20 journal lines.
- **A run that never happens** → nothing fails, so there is nothing to hook. `touch_last_success()`
  writes `$MIRROR_STATE_DIR/last-success` after every fully-successful tick, and fleet-audit
  asserts on its age (< 26h) from outside this process. That is the only place an alert about a
  dead mirror can honestly live.

The **loop mode** (no `--once`) exists for anywhere without a scheduler, or a sub-minute interval.
It costs more: `mirror/healthcheck.sh` as a container `HEALTHCHECK` so the runtime restarts a
wedged-but-running process, plus a separate staleness check for the alert:

```dockerfile
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=2 \
    CMD /app/mirror/healthcheck.sh
```

A wedged long-running process never exits to be noticed, and an alert that shares a process with
the thing it watches is not an alert.

**Not covered:** whole-host death. Accepted — it would be evident from everything else being down,
and fleet-audit's staleness assertion catches it anyway.

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

Everything installs on **infra-box**, as root. Nothing is a Coolify resource.

1. `/etc/buzz-mirror/env`, `0600`, root-owned — see [Configuration](#configuration).
2. `deploy/run-once.sh` and `deploy/alert.sh` → `/opt/buzz-mirror/`, `0700`.
3. The three unit files → `/etc/systemd/system/`, then
   `systemctl daemon-reload && systemctl enable --now buzz-mirror.timer`.
4. `systemctl start buzz-mirror.service` once by hand and read the journal before trusting the
   timer.

**Steps 2 and 3 should not stay manual.** aei owns host plumbing through
`host/install-fleet-units.sh` (units in `host/units/`, run by the converge, idempotent, and the
recovery path after a box rebuild) — and its header states the rule directly: *"Tofu deliberately
does NOT manage these: its providers speak Coolify and GitHub; host plumbing follows the repo's
own 'idempotent scripts, not command history' contract."* So these units belong in aei
`host/units/` and the two scripts in aei `host/`, exactly like `fleet-dispatch.service` and
`fleet-dispatch.py`. This repo keeps what it is good for — the mirror program and its image.
Only `/etc/buzz-mirror/env` stays hand-seeded, because it holds secrets and aei never templates
credentials.

**Both prerequisites now exist in the infra repo (`agentic-engineering-infra`, `dbffa83`):**

- **The token issuer** is `host/gh-app-token.sh` on infra-box, installed as
  `${GH_APP_TOKEN_CMD:-/usr/local/bin/gh-app-token}`. It mints from the App PEM at
  `/root/gh-app/app.pem`, which stays on that box and is read by nothing else.
- **The image** comes from the fleet build pipeline, which is now *derive, don't declare*
  (`wiki/architecture/fleet-pipeline.md`): the central `fleet-build.yml` is gone and each repo
  carries its own `.fleet-build.yml`. Ours declares `deploy: none` — required, because the
  dispatcher treats zero matched Coolify apps as a failure unless build-only is explicit. The
  dispatcher prefixes the `lc(owner)/lc(repo)/` namespace, so the tag is:

  ```
  build-box.prairiedog-tet.ts.net:5000/gitcoinco/buzz-mirror-bot/mirror:<short-sha>
  ```

  The App is installed on `gitcoinco` (installation `152473360`) so the dispatcher can clone
  with a token scoped to the repo's own owner. That installation is for *building*, not
  mirroring — see below.

**Still missing for an automatic build:** a GitHub webhook on `gitcoinco/buzz-mirror-bot` →
`https://hooks.infra.gitcoin.co/build/gitcoinco/buzz-mirror-bot`, with its HMAC secret in
infra-box `/root/fleet-dispatch/secrets/gitcoinco_buzz-mirror-bot`. In aei that means a second
`github` provider alias for `gitcoinco` alongside the `irlfund` one in `tofu/github.tf`. Until it
exists, the image is built by hand or by re-delivering a push.

### Do not let the mirror mirror itself

`buzz-mirror-bot`'s own image is built from its GitHub `main`, and GitHub would be fed by the
mirror. If the mirror breaks, a fix pushed to Buzz cannot reach GitHub, so it cannot be built, so
it stays broken.

**Keep `.github/workflows/buzz-mirror-main.yml` on `buzz-mirror-bot` specifically.** Everywhere
else it is exactly the per-repo cost this replaces and should be deleted once the mirror is proven.
Here it is not redundancy — it is the only thing that breaks the deadlock.

The exclusion is also structural, and it is worth naming because it is easy to break by accident.
`buzz-mirror-bot` satisfies the *Buzz* half of enrolment today — it is announced under
`BUZZ_REPO_OWNER` with a `web` tag pointing at `github.com/gitcoinco/buzz-mirror-bot`. What keeps
it out of the mirror set is the *GitHub* half: a token is only ever valid for one installation, so
`GITHUB_OWNER` names exactly one account and `/installation/repositories` can only ever answer with
repos from that one. Enrolment is therefore "granted to the App **in the `GITHUB_OWNER`
installation**", and `gitcoinco` is not it.

So the App can be installed on `gitcoinco` to build the mirror image without enrolling the mirror
in itself. The thing that would break the deadlock protection is pointing `GITHUB_OWNER` at
`gitcoinco`, not granting the App there.

### Configuration

| var | what |
|---|---|
| `MIRROR_IMAGE` | the image `run-once.sh` runs. **Required** |
| `GITHUB_OWNER` | the App installation to mirror, e.g. `irlfund`. **Required** |
| `BUZZ_PRIVATE_KEY` | mirror-bot's nostr key. Must be a member of every bound channel |
| `BUZZ_AUTH_TAG` | NIP-OA owner attestation |
| `BUZZ_REPO_OWNER` | 64-char hex pubkey that announced the repos |
| `MIRROR_ALERT_CHANNEL` | channel UUID for halt, recovery and adoption messages |
| `GH_APP_TOKEN_CMD` | *optional.* Issuer path. Default `/usr/local/bin/gh-app-token` |
| `MIRROR_INTERVAL_SECS` | loop mode only; ignored by `--once`. Default 60 |

**No GitHub credential appears here.** `run-once.sh` mints
`GITHUB_INSTALLATION_TOKEN` per run and passes it with `-e GITHUB_INSTALLATION_TOKEN` — no value
on the command line, so it never lands in `argv` or `ps`. It expires in an hour whatever happens
to it.

That token must be **installation-wide**: `run-once.sh` calls the issuer with `--owner
"$GITHUB_OWNER"` and no `--repos`. The issuer can narrow a token to a repo list, which is right for
build-box — it only needs `contents: read` on the repos it builds — and wrong here: the mirror set
*is* the App's grants, so a narrowed token would quietly move enrolment out of the App install UI
and into a config file. Granted means fully enrolled.

`--owner` is not optional in practice. A token is only valid for the installation that issued it,
and the App is installed on more than one account, so the issuer's omit-when-there-is-exactly-one
shortcut does not apply.

**There is no repo list.** `MIRROR_REPOS` and `MIRROR_ONLY` are both gone; setting either is a
hard startup error rather than a silently ignored value. See
[What gets mirrored](#what-gets-mirrored).

<details>
<summary>The PEM fallback, for a deployment with no issuer</summary>

Set `GITHUB_APP_ID` and `GITHUB_APP_PEM_B64` instead of a token. The installation id is *not*
configured — it is discovered from the PEM via `GET /app/installations`, and an App set to
"Any account" gets one installation per account, each needing its own token.

The PEM arrives as an environment variable and the entrypoint moves it to a `0400` tmpfs file
before anything else runs, then unsets it. That is weaker than the token path in the way that
matters: a PEM does not expire.

Use `GITHUB_APP_PEM_B64`. A PEM is multi-line and secret-store fields are not, so a raw paste is
the most likely setup mistake there is. `GITHUB_APP_PEM` is also accepted for a single-line value
with literal `\n` escapes; both normalise to the same bytes. The entrypoint checks for
`BEGIN`/`END` markers and refuses to start on a mangled value rather than failing later with an
opaque JWT error.
</details>

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
