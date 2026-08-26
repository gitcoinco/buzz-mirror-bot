#!/bin/sh
# Get a GitHub credential into place, then run the mirror.
#
# The deployed shape supplies tokens: infra-box's issuer holds the App's PEM and
# hands out installation tokens that expire in an hour. Nothing to unpack, and
# no long-lived key is ever inside this container. Either GITHUB_OWNERS with one
# GITHUB_INSTALLATION_TOKEN_<OWNER> per named account, or the older single
# GITHUB_INSTALLATION_TOKEN.
#
# The PEM path below is the fallback for a deployment with no issuer - loop mode
# somewhere else, or a laptop. There the key arrives as an env var, which is
# readable from /proc/<pid>/environ by anything running as this user and shows
# up in any accidental `env` dump, so it is moved to a 0400 tmpfs file and
# unset. That is a mitigation, not a fix; prefer the token.

set -eu

# GITHUB_OWNERS is checked as well as the singular token, and it has to be:
# with two or more accounts the launcher deliberately leaves the singular unset
# (an older image would read it and mirror only one account). Testing only the
# singular sent the multi-account shape down the PEM branch, where it exited 1
# asking for a key that is not supposed to be in this container.
if [ -n "${GITHUB_INSTALLATION_TOKEN:-}" ] || [ -n "${GITHUB_OWNERS:-}" ]; then
    if [ -n "${BUZZ_PRIVATE_KEY:-}" ] && [ -z "${NOSTR_PRIVATE_KEY:-}" ]; then
        NOSTR_PRIVATE_KEY="$BUZZ_PRIVATE_KEY"
        export NOSTR_PRIVATE_KEY
    fi
    exec python3 /app/mirror/sync.py "$@"
fi

PEM_PATH="${GITHUB_APP_PEM_PATH:-/run/mirror/ghapp.pem}"

mkdir -p "$(dirname "$PEM_PATH")"
umask 077

# The previous run left this 0400, so it cannot simply be overwritten. Removing
# first keeps the entrypoint idempotent across restarts.
rm -f "$PEM_PATH" "$PEM_PATH.raw"

# A PEM is multi-line and most secret stores - Coolify's env var field included -
# are single-line. Two ways in, and base64 is the one to prefer: it survives any
# form field, any shell, and any copy-paste without an escaping question.
if [ -n "${GITHUB_APP_PEM_B64:-}" ]; then
    printf '%s' "$GITHUB_APP_PEM_B64" | base64 -d > "$PEM_PATH.raw"
    unset GITHUB_APP_PEM_B64
elif [ -n "${GITHUB_APP_PEM:-}" ]; then
    # %b, not echo: expands the literal \n sequences a single-line PEM carries.
    printf '%b\n' "$GITHUB_APP_PEM" > "$PEM_PATH.raw"
    unset GITHUB_APP_PEM
else
    echo "set GITHUB_APP_PEM_B64 (preferred) or GITHUB_APP_PEM" >&2
    exit 1
fi

# Drop blank lines so both paths produce byte-identical output regardless of how
# the value was escaped on the way in. A PEM body contains none, so this only
# ever removes an artefact of the encoding.
awk 'NF' "$PEM_PATH.raw" > "$PEM_PATH"
rm -f "$PEM_PATH.raw"
chmod 400 "$PEM_PATH"

# Fail here rather than 40 minutes later on a confusing JWT error. A mangled
# multi-line paste is the single most likely setup mistake.
if ! grep -q -- "-----BEGIN" "$PEM_PATH" || ! grep -q -- "-----END" "$PEM_PATH"; then
    echo "PEM at $PEM_PATH is not a valid key - check the value survived pasting" >&2
    exit 1
fi

# git-credential-nostr reads NOSTR_PRIVATE_KEY; the buzz CLI reads
# BUZZ_PRIVATE_KEY. Same key, two names - set one from the other so the
# deployment only has to supply it once.
if [ -n "${BUZZ_PRIVATE_KEY:-}" ] && [ -z "${NOSTR_PRIVATE_KEY:-}" ]; then
    NOSTR_PRIVATE_KEY="$BUZZ_PRIVATE_KEY"
    export NOSTR_PRIVATE_KEY
fi

# "$@" so the same image serves both modes. The deployed shape is one container
# per run - `docker run --rm <image> --once`, started by a systemd timer on
# infra-box - so there is nothing to hold open and no exec target to keep alive.
#
# No args loops instead, for anywhere without a scheduler; `--once` runs a
# single reconcile and exits non-zero on failure.
exec python3 /app/mirror/sync.py "$@"
