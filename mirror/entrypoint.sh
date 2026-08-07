#!/bin/sh
# Move the GitHub App private key out of the environment and onto a tmpfs file
# before anything else runs.
#
# Coolify delivers secrets as environment variables, and an env var is readable
# from /proc/<pid>/environ by anything running as this user and shows up in any
# accidental `env` dump. A 0400 file on tmpfs is the closest we get to systemd's
# LoadCredential here. This is a real, accepted downside of deploying via
# Coolify rather than a systemd unit: the key transits an env var on its way in.
#
# Unsetting it after writing means the daemon process itself never carries it.

set -eu

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

# "$@" so the same image serves both modes: no args loops, `--once` runs a
# single reconcile and exits non-zero on failure (for a scheduled task).
exec python3 /app/mirror/daemon.py "$@"
