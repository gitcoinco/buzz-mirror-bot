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

if [ -z "${GITHUB_APP_PEM:-}" ]; then
    echo "GITHUB_APP_PEM is not set" >&2
    exit 1
fi

mkdir -p "$(dirname "$PEM_PATH")"
umask 077
# Printf, not echo: the PEM arrives with literal \n from most secret stores.
printf '%b\n' "$GITHUB_APP_PEM" > "$PEM_PATH"
chmod 400 "$PEM_PATH"
unset GITHUB_APP_PEM

# git-credential-nostr reads NOSTR_PRIVATE_KEY; the buzz CLI reads
# BUZZ_PRIVATE_KEY. Same key, two names - set one from the other so the
# deployment only has to supply it once.
if [ -n "${BUZZ_PRIVATE_KEY:-}" ] && [ -z "${NOSTR_PRIVATE_KEY:-}" ]; then
    NOSTR_PRIVATE_KEY="$BUZZ_PRIVATE_KEY"
    export NOSTR_PRIVATE_KEY
fi

exec python3 /app/mirror/daemon.py
