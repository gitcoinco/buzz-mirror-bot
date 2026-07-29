# Mirroring a Buzz-hosted repo's `main` to GitHub

Buzz is the primary origin. GitHub is a read-only mirror of `main`, kept current by a scheduled
GitHub Action that *pulls* from Buzz. Direction is one-way, fast-forward only.

**Why this shape:** no GitHub write credential exists anywhere outside GitHub. The Action pushes
with the built-in `GITHUB_TOKEN`. The only stored secret is a Buzz *read* key, which grants
nothing on GitHub — worst case someone reads one channel's repos.

Workflow: [`.github/workflows/buzz-mirror-main.yml`](../.github/workflows/buzz-mirror-main.yml).
To use it on another repo, copy that file to the same path there and edit the two `env:` values.

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

### 4. Seed the Buzz repo from GitHub

One-time, done by a human who already holds GitHub read access — no agent involved:

```sh
git clone git@github.com:<org>/<repo>.git && cd <repo>
git remote add buzz https://buzz.gitcoin.co/git/<owner-hex>/<repo>.git
NOSTR_PRIVATE_KEY=<your-nsec> git push buzz refs/heads/main:refs/heads/main
```

### 5. Fill in the workflow

Edit the two `env:` values at the top — `BUZZ_REPO_URL` (owner is the 64-char hex pubkey that
announced the repo, not a username) and `BUZZ_COMMIT` (pin for the credential-helper build).
Then run it once via **Actions → Mirror main from Buzz → Run workflow**.

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
