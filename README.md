# buzz-mirror-bot

Keep a GitHub repo's `main` in sync with a [Buzz](https://github.com/block/buzz)-hosted git
origin, without any GitHub write credential existing outside GitHub.

This repo is its own first user: its origin is Buzz, and the workflow here mirrors that origin to
`github.com/gitcoinco/buzz-mirror-bot`.

## The idea

Buzz relays host real git repos over smart HTTP, authenticated with a nostr key (NIP-98) instead
of a password. That makes Buzz a fine primary origin — agents and humans push there — but you
still want GitHub current for the humans, tooling, and CI that live there.

Two mechanisms live here. **Start with the mirror** ([`docs/MIRROR.md`](docs/MIRROR.md)) — the
Action came first and taught us the shape, but it costs a workflow file and two secrets *in every
mirrored repo*, which does not scale.

| | Action | Mirror |
|---|---|---|
| per-repo cost | workflow file + 2 secrets | none |
| adding a repo | commit + secrets | tick the repo in the App's install UI |
| GitHub write credential | none — uses `GITHUB_TOKEN` | a GitHub App key, off-platform |
| latency | ~10 min | your cron interval |
| GitHub ahead | fails red | fast-forwards Buzz to it, or proposes a branch + PR where Buzz `main` is not writable |

The Action's one real advantage is that no GitHub write credential exists anywhere:

> A scheduled GitHub Action **pulls** from Buzz and fast-forwards `main`, pushing with the
> built-in `GITHUB_TOKEN`.

Its only secret is a Buzz *read* key for a dedicated `mirror-bot` identity. The mirror gives that
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

- **Buzz-first, not Buzz-only.** Buzz → GitHub is the normal direction. The mirror will also
  fast-forward Buzz to GitHub's tip when GitHub is strictly ahead, because that is an update
  rather than a conflict; see [`docs/MIRROR.md`](docs/MIRROR.md) for what that costs. The Action
  is one-way and stays that way.
- **Fast-forward only.** Every push is plain and non-force, so nothing here can rewrite history
  in either direction. If the two `main`s genuinely diverge, it halts and asks a human.
- **`main` only.** Other branches stay on Buzz.
- **Least-privilege identity.** `mirror-bot` is a distinct nostr key added to the repo's bound
  channel as a `bot`, carrying a NIP-OA attestation from its owner. Buzz-side branch protection
  is what bounds it: `push:member` lets it fast-forward `main` and nothing else, and the relay's
  unweakenable defaults keep force-push and delete at Admin. A repo that should never be written
  from GitHub keeps `push:owner`, and the mirror proposes instead.

## Layout

| Path | What |
|---|---|
| `mirror/sync.py` | the mirror — one deployment, any number of repos |
| `mirror/healthcheck.sh` | staleness check; loop mode only |
| `mirror/test_reconcile.py` | ancestry cases, discovery and locking against real repos |
| `Dockerfile`, `docker-compose.yml` | the Coolify application |
| `docs/MIRROR.md` | how the mirror works and why it is shaped this way |
| `.github/workflows/buzz-mirror-main.yml` | the original Action — superseded by the mirror |
| `docs/SETUP.md` | setup, and the sharp edges worth knowing first |
| `buzz-agent-identity.py` | provision a Buzz agent identity — fresh key + NIP-OA owner attestation, pure stdlib |

Start with [`docs/MIRROR.md`](docs/MIRROR.md); [`docs/SETUP.md`](docs/SETUP.md) covers the Buzz-side
git sharp edges that apply to both.

## Status

The Buzz half is verified against a live relay: channel binding, clone, push, ref-state events,
and all four sync paths (create / in-sync / fast-forward / diverged). The Action is exercised by
this repo — if `main` here matches Buzz, it works.

The mirror's decision tree and its repo discovery are covered by `mirror/test_reconcile.py`
against real repos. It has **not** yet run against the live relay or a real GitHub App — that
needs the App to exist.
