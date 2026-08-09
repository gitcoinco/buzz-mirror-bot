#!/bin/bash
# Say in Buzz that a mirror run failed. Called only by
# buzz-mirror-alert@.service, with the failed unit name as $1.
#
# Deliberately thin: the journal already has the detail, and an alert that tries
# to summarise a failure it did not see is how alerts start lying. This says
# what failed, when, and where to look.
#
# If the fleet has a shared poster (fleet-audit's chip_post), point this at it
# instead - the value here is the OnFailure wiring, not this script.
set -uo pipefail

CONF=${BUZZ_MIRROR_ENV:-/etc/buzz-mirror/env}
# shellcheck disable=SC1090
[ -r "$CONF" ] && { set -a; . "$CONF"; set +a; }

UNIT=${1:-buzz-mirror.service}
: "${MIRROR_ALERT_CHANNEL:?}"

printf '**Mirror run failed** (`%s` on infra-box)\n\nLast 20 journal lines:\n\n```\n%s\n```\n' \
    "$UNIT" "$(journalctl -u "$UNIT" -n 20 --no-pager -o cat 2>&1)" \
  | buzz messages send --channel "$MIRROR_ALERT_CHANNEL" --content -
