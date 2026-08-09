#!/bin/sh
# Staleness check on the last fully-successful tick.
#
# LOOP MODE ONLY. The deployed shape is `--once` from a systemd timer, where a
# failed run alerts through the unit's OnFailure= and a timer that stops firing
# is caught by fleet-audit asserting on the same `last-success` file from
# outside. This script is not wired into the image; docs/MIRROR.md says how to
# add it back if you run the loop.
#
# It exists because a wedged long-running process never exits to be noticed. Use
# it in two places, deliberately the same script:
#   1. the container HEALTHCHECK -> the runtime restarts a wedged-but-running loop
#   2. an out-of-band scheduled check -> the actual alert
#
# (2) is the one that matters. An alert emitted by the mirror itself dies with
# it, and the sharpest wedge mode - the Buzz key being dropped from a channel -
# takes out its ability to post at the same moment it takes out its ability to
# fetch. This check has to run in a different process, on a different schedule.
#
# Exit 0 = fresh, exit 1 = stale. Nothing else.

set -eu

STATE_DIR="${MIRROR_STATE_DIR:-/var/lib/git-mirror}"
LAST="$STATE_DIR/last-success"

# Three missed ticks before calling it stale, so one slow fetch or a single
# GitHub blip does not trigger a restart.
INTERVAL="${MIRROR_INTERVAL_SECS:-60}"
MAX_AGE="${MIRROR_MAX_AGE_SECS:-$((INTERVAL * 3))}"

if [ ! -f "$LAST" ]; then
    echo "no successful tick yet ($LAST missing)" >&2
    exit 1
fi

age=$(( $(date +%s) - $(cat "$LAST") ))

if [ "$age" -gt "$MAX_AGE" ]; then
    echo "last successful tick was ${age}s ago (max ${MAX_AGE}s) - mirror is stale" >&2
    exit 1
fi

echo "ok: last success ${age}s ago"
