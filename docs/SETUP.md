# Mirroring a Buzz-hosted repo's `main` to GitHub

Buzz is the primary origin. GitHub is a read-only mirror of `main`, kept current by a scheduled
GitHub Action that *pulls* from Buzz. Direction is one-way, fast-forward only.

**Why this shape:** no GitHub write credential exists anywhere outside GitHub. The Action pushes
with the built-in `GITHUB_TOKEN`. The only stored secret is a Buzz *read* key, which grants
nothing on GitHub — worst case someone reads one channel's repos.

The workflow file was removed from this repo on 2026-09-04; the mirror daemon
([`MIRROR.md`](MIRROR.md)) carries every enrolled repo, this one included. The last copy is
`.github/workflows/buzz-mirror-main.yml` in commit `6333177`. To use the Action shape on a repo
the mirror does not cover, copy that file from there and edit the two `env:` values.

## One-time setup

### 1. Create the mirror-bot Buzz identity

Run `buzz-agent-identity.py` **on your own machine** so the nsec never touches an agent session:

```sh
./buzz-agent-identity.py provision --name mirror-bot --out mirror-bot.env
```

That generates a fresh keypair *and* signs a NIP-OA owner attestation, writing `BUZZ_PRIVATE_KEY`
+ `BUZZ_AUTH_TAG` to a 0600 env file. Both parts matter and they are not alternatives:

- **Its own key** is mandatory — the channel membership row and the git read gate key off the
  bot's own pubkey, with no owner fallback.
- **The attestation** is what gets it through the relay's membership door via your membership,
  so you never enroll it as a standalone community member. The relay materializes the
  agent→owner mapping automatically on first authenticated request; there is no enrollment step.

Then add the bot's hex pubkey to the channel the repo is bound to — the attestation does *not*
substitute for this:

```sh
buzz channels members --channel <channel-uuid>   # confirm it landed
```

### 2. Store the secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `BUZZ_MIRROR_NSEC` | the mirror-bot `BUZZ_PRIVATE_KEY` |
| `BUZZ_MIRROR_AUTH_TAG` | the mirror-bot `BUZZ_AUTH_TAG` (the `["auth",…]` JSON) |

The auth tag is not itself an authenticator — it is useless without the nsec — but store it as a
secret anyway rather than inlining it in the workflow.

### 3. Bind the repo to the channel and protect `main`

Neither is optional. Without a `buzz-channel` tag on the repo's `kind:30617` announcement, **every
git operation returns `404 repository not found`** — clone and push, for everyone including the
repo's own owner. The relay resolves the tag to a channel and requires the caller to be an active
member of it (`authorize_git_read`, and separately the pre-receive policy hook).

At the time of writing no shipped Buzz client writes that tag: `buzz repos create` has no
`--channel` flag, and the web UI only reads it. So you must publish the announcement yourself,
with the tag included. The relay does not validate the tag at ingest, and re-announcing the same
`d` under the same pubkey is idempotent, so this is safe to redo:

1. Build a `kind:30617` event with tags `d`, `name`, `description`, `clone`, `buzz-channel`, and
   any `buzz-protect` rules (see below).
2. Sign it with the repo owner's nostr key.
3. `POST /events` with a NIP-98 header: `Authorization: Nostr <base64(signed kind:27235 event)>`.

Set protection rules in that *same* announcement. `buzz repos protect set` rebuilds the event with
`created_at = existing.created_at + 1`, and the relay rejects timestamps drifting more than ±900s
from server time — so it fails on any repo more than 15 minutes old.

```sh
buzz repos protect list --id <repo>
# expect: refs/heads/main -> push:owner, no-force-push, no-delete
```

Rule vocabulary: `["buzz-protect", "<ref-pattern>", "<rule>", …]` where rule is
`push:<owner|admin|member>`, `no-force-push`, `no-delete`, or `require-patch`.

`push:member` is the one to reach for if you want the mirror to adopt a GitHub-ahead `main`
automatically (see [`MIRROR.md`](MIRROR.md)). It is narrower than it sounds: the relay takes
`max(explicit push:role, default_min_role(ref, kind))`, and the built-in defaults on a branch are
Member for a fast-forward but Admin for a non-fast-forward or a delete — which an explicit rule
can never weaken. So `push:member` + `no-force-push` + `no-delete` means "may fast-forward, may
do nothing else". Keep `push:owner` on any repo that should never be written from GitHub.

### 4. Get the two histories connected — once, by hand

**The mirror cannot bootstrap itself.** GitHub only exposes a workflow — both its
`workflow_dispatch` "Run workflow" button and its `schedule` triggers — once the file exists on
the repo's **default branch**. An empty GitHub repo therefore has no workflow to run, and no way
to run one. Something has to put the first commit there by hand.

Which direction depends on where the repo started.

**Repo already exists on GitHub** — push it up to Buzz. Done by a human who already holds GitHub
read access; no agent involved:

```sh
git clone git@github.com:<org>/<repo>.git && cd <repo>
git remote add buzz https://buzz.gitcoin.co/git/<owner-hex>/<repo>.git
NOSTR_PRIVATE_KEY=<your-nsec> git push buzz refs/heads/main:refs/heads/main
```

**Any push to Buzz larger than 1 MB needs `-c http.postBuffer=524288000`.** Above git's default
`http.postBuffer`, the POST switches to chunked transfer encoding and the relay answers `HTTP 401`
— with a misleading trailing `Everything up-to-date`. It is not a credentials problem: the same
key succeeds on a smaller push and fails in about two seconds on a larger one.


**Repo was born on Buzz** (like this one) — push it down to GitHub once. Create the GitHub repo
empty, with no README and no initial commit, then:

Reading from Buzz needs the credential helper registered first — see
[Cloning from Buzz](#cloning-from-buzz) below, and do that before the clone.

```sh
export NOSTR_PRIVATE_KEY=<your-nsec>
git clone https://buzz.gitcoin.co/git/<owner-hex>/<repo>.git && cd <repo>
git remote add gh git@github.com:<org>/<repo>.git
git push gh main
```

That single push is the whole bootstrap: it creates `main` on GitHub *and* lands the workflow
file on the default branch, at which point the Action appears in the UI and takes over. Every
later sync is automatic.

If you'd rather not install the helper, have someone who already has Buzz access hand you a
`git bundle` of the repo — `git clone <bundle> <repo>` gives you a normal checkout with full
history to push to GitHub.

#### Cloning from Buzz

Buzz git authenticates with a nostr key over NIP-98, not a password. Three things must all be
true or git silently falls back to prompting for a username and password, which will never work:

```sh
# 1. git >= 2.46. Older git never sends the credential protocol's
#    capability[]=authtype line, so the helper returns nothing and git prompts.
#    macOS Xcode command-line-tools git is often too old; `brew install git` is current.
git --version

# 2. The helper is built and on PATH as `git-credential-nostr`.
cargo install --git https://github.com/block/buzz git-credential-nostr --locked
command -v git-credential-nostr

# 3. git is told to use it, with per-path credentials.
#    useHttpPath matters: the NIP-98 token is scoped to the repo path, not the host.
git config --global credential.helper nostr
git config --global credential.useHttpPath true
```

Then `export NOSTR_PRIVATE_KEY=<nsec>` (or `git config --global nostr.keyfile ~/.nostr/key` with
a 0600 file) and clone normally.

**Without touching global config.** Pass the settings to the one command instead. A helper value
containing `/` is run as a path; one without is looked up as `git-credential-nostr` on `PATH`.

```sh
# Scoped to the new clone — written to its .git/config, so later fetches keep working.
git clone --config credential.helper=nostr \
          --config credential.useHttpPath=true \
          https://<relay-host>/git/<owner-hex>/<repo>.git

# Or fully transient: applies to this invocation only, persists nowhere.
git -c credential.helper=nostr -c credential.useHttpPath=true clone <url>

# Or with no flags at all, e.g. inside a script.
GIT_CONFIG_COUNT=2 \
GIT_CONFIG_KEY_0=credential.helper   GIT_CONFIG_VALUE_0=nostr \
GIT_CONFIG_KEY_1=credential.useHttpPath GIT_CONFIG_VALUE_1=true \
  git clone <url>
```

A username prompt means one of the three above is missing — the helper cannot prompt, so any
failure to engage looks like ordinary HTTP basic auth. Check them in order.

#### Symptom → cause

All four seen in practice against a live relay:

| Symptom | Cause |
|---|---|
| prompts for a username | the helper isn't engaging: git < 2.46, not on `PATH`, or `credential.helper` unset. It cannot prompt, so any non-engagement looks like ordinary basic auth |
| `403 restricted: not a relay member` | the key isn't a relay member and no `BUZZ_AUTH_TAG` was presented. Channel membership does **not** satisfy this — separate gate, checked first |
| `404 repository not found` | no `buzz-channel` tag on the announcement, or the caller isn't an active member of the bound channel |
| `401` + `send-pack: unexpected disconnect` + a trailing `Everything up-to-date` | the pack exceeds `http.postBuffer`. **Not** credentials — the same key succeeds on a smaller push |

The recommended invocation covers all of them. The empty `credential.helper=` matters because the
setting is additive: without the reset, a globally configured helper (macOS keychain, cache) is
consulted first and can answer with a username/password that the relay rejects — and a caching
helper will also try to store the time-bound NIP-98 token, which is useless a minute later.

```sh
git -c credential.helper= \
    -c credential.helper=nostr \
    -c credential.useHttpPath=true \
    -c http.postBuffer=524288000 \
    push buzz main
```

`git ls-remote <url>` is the cheap probe — it exercises auth without transferring anything, and
exit 0 with zero refs means live-and-empty rather than broken.

#### Two independent gates

`403 restricted: not a relay member` is a *different* failure — it means the helper worked and
the relay rejected the identity. Buzz checks two separate things, in this order:

1. **Relay membership** (`relay_members` table). Either the key is an accepted member itself, or
   it presents a NIP-OA auth tag whose *owner* is a member.
2. **Channel membership** of the repo's bound channel.

Adding a key to a channel does **not** make it a relay member — different tables, different
gates. A provisioned agent identity is normally not a relay member in its own right, so its auth
tag is load-bearing on **every** request, not a one-time enrolment: the relay records the
agent→owner mapping in `users`, but never adds the agent to `relay_members`.

So an agent key needs both env vars, and the tag contains spaces — quote it or the shell eats it:

```sh
export NOSTR_PRIVATE_KEY='nsec1…'
export BUZZ_AUTH_TAG='["auth", "<owner-hex>", "", "<sig>"]'   # single quotes required
```

Equivalently, `git clone --config nostr.authtag='["auth", …]'`.

For a one-off human clone it is simpler to use your own key, which is usually a relay member
directly and needs no tag at all.

### 5. Fill in the workflow

Edit the two `env:` values at the top — `BUZZ_REPO_URL` (owner is the 64-char hex pubkey that
announced the repo, not a username) and `BUZZ_COMMIT` (pin for the credential-helper build). If
you bootstrapped from Buzz these are already correct.

After the bootstrap push, confirm it works end to end via **Actions → Mirror main from Buzz →
Run workflow**. It should report `already in sync` — that is the success case, and it proves
mirror-bot can authenticate and read before you depend on the schedule.

## How it behaves

| Situation | Result |
|---|---|
| `main` absent on GitHub | creates it |
| Buzz `main` == GitHub `main` | no-op, exits 0 |
| Buzz `main` ahead | fast-forwards GitHub `main` |
| Histories diverged | **fails loudly, never force-pushes** — reconcile by hand |

All four verified against the live relay on 2026-07-29 using the `mirror-probe` repo.

## Known sharp edges

- **Cron is best-effort.** GitHub delays scheduled runs under load; `*/10` means "usually within
  10–30 min". Use **Run workflow** when you need it now.
- **Scheduled workflows auto-disable** after 60 days without repo activity. GitHub emails first.
- **Protected `main` on GitHub** can reject `GITHUB_TOKEN` pushes. Either exempt the token in
  branch-protection rules, or accept that this mirror only works on an unprotected mirror branch.
- **First run builds the credential helper** (`cargo install --git … --locked`, ~3 min measured).
  Subsequent runs restore it from cache in seconds. The cache key includes `BUZZ_COMMIT`, so
  bumping the pin triggers one rebuild.
- **git ≥ 2.46 required** — the helper silently no-ops on older git because the credential
  protocol never sends `capability[]=authtype`. The workflow asserts this and fails fast.
- **NIP-OA `conditions` are not enforced on the git path.** The grammar supports expiry
  (`created_at<<unix-ts>`) and `kind=` scoping, and `verify_auth_tag` validates the syntax and
  signs over it — but the only code that actually *enforces* a time bound is
  `enforce_request_auth_time_bounds`, called from one handler (`identity_archive.rs:266`). The git
  transport and relay-membership paths never check it. So an expiring attestation is decorative
  here: revoke by removing the bot from the channel and rotating the key, not by setting an expiry.
- **Anything but `main` is not mirrored.** Non-main redundancy is agents' local checkouts + the
  Buzz copy + the S3 backups of the whole system.
