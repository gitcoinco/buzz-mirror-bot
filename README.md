# buzz-mirror-bot

Keep a GitHub repo's `main` in sync with a [Buzz](https://github.com/block/buzz)-hosted git
origin, without any GitHub write credential existing outside GitHub.

This repo is its own first user: its origin is Buzz, and the workflow here mirrors that origin to
`github.com/gitcoinco/buzz-mirror-bot`.

## The idea

Buzz relays host real git repos over smart HTTP, authenticated with a nostr key (NIP-98) instead
of a password. That makes Buzz a fine primary origin — agents and humans push there — but you
still want GitHub current for the humans, tooling, and CI that live there.

The obvious approach is a daemon holding a GitHub deploy key. This does the opposite:

> A scheduled GitHub Action **pulls** from Buzz and fast-forwards `main`, pushing with the
> built-in `GITHUB_TOKEN`.

Nothing that can write to GitHub is ever stored anywhere. The only secret is a Buzz *read* key
for a dedicated `mirror-bot` identity — if it leaks, someone can read one channel's repos, and
you rotate it by removing that identity from the channel.

```
  agent / human                 Buzz relay                    GitHub
       │                            │                            │
       │ git push ─────────────────►│                            │
       │                            │                            │
       │                            │◄─── git fetch ─────────────│  scheduled Action
       │                            │                            │  (GITHUB_TOKEN)
       │                            │      fast-forward main ────►│
```

## Properties

- **One-way.** Buzz → GitHub. GitHub `main` is never the source of truth here.
- **Fast-forward only.** The push is plain and non-force, so it cannot rewrite history. If the
  two `main`s diverge, the job fails loudly and asks a human to reconcile.
- **`main` only.** Other branches stay on Buzz.
- **Least-privilege identity.** `mirror-bot` is a distinct nostr key added to the repo's bound
  channel as a `bot`, carrying a NIP-OA attestation from its owner. Buzz-side branch protection
  (`push:owner`) keeps it off `main` there.

## Layout

| Path | What |
|---|---|
| `.github/workflows/buzz-mirror-main.yml` | the mirror job |
| `docs/SETUP.md` | setup, and the sharp edges worth knowing first |
| `buzz-agent-identity.py` | provision a Buzz agent identity — fresh key + NIP-OA owner attestation, pure stdlib |

Start with [`docs/SETUP.md`](docs/SETUP.md).

## Status

The Buzz half is verified against a live relay: channel binding, clone, push, ref-state events,
and all four sync paths (create / in-sync / fast-forward / diverged). The Action itself is
exercised by this repo — if the badge-less `main` here matches Buzz, it works.
