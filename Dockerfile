# buzz-mirror-daemon
#
# Two binaries have to come from the buzz workspace and are pinned to one
# commit, so a bad upstream change cannot silently alter auth behaviour:
#   git-credential-nostr  - NIP-98 auth for git against the relay
#   buzz                  - channel posts and NIP-34 PRs
#
# Base is trixie because git-credential-nostr silently no-ops on git < 2.46:
# older git never sends the credential protocol's `capability[]=authtype` line,
# so the helper is never asked for anything and every push prompts for a
# username instead. The assert below turns that into a build failure rather
# than a confusing runtime one.

ARG BUZZ_COMMIT=24d90d1280a9325c6cbcf8eea30ac54db5afd2cb

FROM rust:1-trixie AS build
ARG BUZZ_COMMIT
RUN cargo install --git https://github.com/block/buzz --rev "${BUZZ_COMMIT}" \
      --locked --root /out git-credential-nostr buzz-cli

FROM python:3.13-slim-trixie
COPY --from=build /out/bin/git-credential-nostr /usr/local/bin/
COPY --from=build /out/bin/buzz /usr/local/bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ver=$(git --version | awk '{print $3}') \
 && [ "$(printf '%s\n2.46.0\n' "$ver" | sort -V | head -1)" = "2.46.0" ] \
      || { echo "git $ver is too old; git-credential-nostr needs >= 2.46" >&2; exit 1; }

RUN pip install --no-cache-dir "PyJWT[crypto]==2.10.1"

# Non-root. The daemon holds a GitHub App key with contents+workflows write and
# a Buzz identity with read on private repos; it has no reason to be root.
# uid pinned, not auto-assigned: docker-compose has to name it in the tmpfs
# mount options, and a mount whose uid does not match is silently unwritable.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mirror \
 && mkdir -p /var/lib/git-mirror /run/mirror \
 && chown mirror:mirror /var/lib/git-mirror /run/mirror

COPY mirror/ /app/mirror/
RUN chmod +x /app/mirror/entrypoint.sh /app/mirror/healthcheck.sh

USER mirror
VOLUME /var/lib/git-mirror

# Interval-derived: three missed ticks before unhealthy. Coolify restarts on
# unhealthy, which converts "wedged but running" into something visible.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=2 \
    CMD /app/mirror/healthcheck.sh

ENTRYPOINT ["/app/mirror/entrypoint.sh"]
