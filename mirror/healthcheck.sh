#!/bin/sh
# Staleness check on the daemon's last fully-successful tick.
#
# Used in two places, deliberately the same script:
#   1. the container HEALTHCHECK  -> Coolify restarts a wedged-but-running daemon
#   2. a Coolify scheduled task   -> Coolify fires "Scheduled Task Failure",
#                                    which is the out-of-band alert path
#
# (2) is the one that matters. An alert emitted by the daemon itself dies with
# the daemon, and the sharpest wedge mode - the Buzz key being dropped from a
# channel - takes out the daemon's ability to post at the same moment it takes
# out its ability to fetch. This check runs in a different process, on a
# different schedule, and alerts through Coolify rather than through Buzz.
#
# Exit 0 = fresh, exit 1 = stale. Nothing else.
#
# NOTE: verify the Coolify notification actually fires, once, with a deliberate
# failure. There are open upstream reports of failure notifications not being
# sent while success notifications are. An alarm you believe in but do not have
# is worse than no alarm.

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
