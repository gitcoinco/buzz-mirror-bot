#!/bin/bash
# One reconcile pass, in a container, on infra-box. Called by
# buzz-mirror.service; safe to run by hand for a one-off.
#
# The mirror holds no long-lived GitHub credential. The App's PEM lives only in
# /root/gh-app/ and only the issuer reads it; this script asks the issuer for an
# installation token and hands that to the container, where it expires in an
# hour whatever happens to it.
set -euo pipefail

# Non-secret settings: MIRROR_IMAGE, GITHUB_OWNER, BUZZ_REPO_OWNER,
# MIRROR_ALERT_CHANNEL. Secrets: BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG. 600, root.
CONF=${BUZZ_MIRROR_ENV:-/etc/buzz-mirror/env}
[ -r "$CONF" ] || { echo "missing $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$CONF"; set +a

: "${MIRROR_IMAGE:?set MIRROR_IMAGE in $CONF}"
: "${GITHUB_OWNER:?set GITHUB_OWNER in $CONF - the App installation to mirror}"
ISSUER=${GH_APP_TOKEN_CMD:-/usr/local/bin/gh-app-token}

# `--owner`, and deliberately no `--repos`.
#
# INSTALLATION-WIDE is the point. The issuer can narrow a token to a repo list,
# which is right for build-box - it only needs contents:read on the repos it
# builds - and wrong here: the mirror set IS the App's grants, so narrowing this
# token would quietly move enrolment out of the App install UI and into this
# file. Granted means fully enrolled, and this is where that is read.
#
# `--owner` is still required, because a token is only ever valid for ONE
# installation. The App is installed on more than one account (lucianearth for
# scratch testing, irlfund for the real repos), so the issuer's
# omit-when-there-is-exactly-one shortcut does not apply and leaving it off is a
# hard error. Naming the owner here also bounds the blast radius of a stray
# grant on an account this mirror has no business reading.
GITHUB_INSTALLATION_TOKEN=$("$ISSUER" --owner "$GITHUB_OWNER")
export GITHUB_INSTALLATION_TOKEN

# `-e VAR` with no value passes it from this process's environment, so the
# token never appears in argv and never shows up in `ps`.
exec docker run --rm \
    --name buzz-mirror-run \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    -v buzz-mirror-state:/var/lib/git-mirror \
    -e GITHUB_INSTALLATION_TOKEN \
    -e BUZZ_PRIVATE_KEY \
    -e BUZZ_AUTH_TAG \
    -e BUZZ_RELAY_URL \
    -e BUZZ_REPO_OWNER \
    -e MIRROR_ALERT_CHANNEL \
    "$MIRROR_IMAGE" --once
