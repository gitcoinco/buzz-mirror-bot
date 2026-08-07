# buzz-mirror-bot

Keep a GitHub repo's `main` in sync with a [Buzz](https://github.com/block/buzz)-hosted git
origin, without any GitHub write credential existing outside GitHub.

This repo is its own first user: its origin is Buzz, and the workflow here mirrors that origin to
`github.com/gitcoinco/buzz-mirror-bot`.

## The idea

Buzz relays host real git repos over smart HTTP, authenticated with a nostr key (NIP-98) instead
of a password. That makes Buzz a fine primary origin — agents and humans push there — but you
still want GitHub current for the humans, tooling, and CI that live there.

Two mechanisms live here. **Start with the daemon** ([`docs/DAEMON.md`](docs/DAEMON.md)) — the
Action came first and taught us the shape, but it costs a workflow file and two secrets *in every
mirrored repo*, which does not scale.

| | Action | Daemon |
|---|---|---|
| per-repo cost | workflow file + 2 secrets | none |
| adding a repo | commit + secrets | one config line + an App install click |
| GitHub write credential | none — uses `GITHUB_TOKEN` | a GitHub App key, off-platform |
| latency | ~10 min | ~60s |
| GitHub ahead | fails red | proposes a branch + PR, halts |

The Action's one real advantage is that no GitHub write credential exists anywhere:

> A scheduled GitHub Action **pulls** from Buzz and fast-forwards `main`, pushing with the
> built-in `GITHUB_TOKEN`.

Its only secret is a Buzz *read* key for a dedicated `mirror-bot` identity. The daemon gives that
up in exchange for scaling: it holds a GitHub App key that can write the repos it is installed on.

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
| `mirror/daemon.py` | the daemon — one container, any number of repos |
| `mirror/healthcheck.sh` | staleness check; container healthcheck *and* Coolify scheduled task |
| `mirror/test_reconcile.py` | all four ancestry cases against real repos |
| `Dockerfile`, `docker-compose.yml` | the Coolify application |
| `docs/DAEMON.md` | how the daemon works and why it is shaped this way |
| `.github/workflows/buzz-mirror-main.yml` | the original Action — superseded by the daemon |
| `docs/SETUP.md` | setup, and the sharp edges worth knowing first |
| `buzz-agent-identity.py` | provision a Buzz agent identity — fresh key + NIP-OA owner attestation, pure stdlib |

Start with [`docs/DAEMON.md`](docs/DAEMON.md); [`docs/SETUP.md`](docs/SETUP.md) covers the Buzz-side
git sharp edges that apply to both.

## Status

The Buzz half is verified against a live relay: channel binding, clone, push, ref-state events,
and all four sync paths (create / in-sync / fast-forward / diverged). The Action is exercised by
this repo — if `main` here matches Buzz, it works.

The daemon's decision tree is covered by `mirror/test_reconcile.py` against real repos. It has
**not** yet run against the live relay or a real GitHub App — that needs the App to exist.
