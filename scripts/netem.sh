#!/usr/bin/env bash
# Purpose: apply / clear tc netem delay and loss profiles for measurements
# (PROJECT_CONTEXT.md §11 condition 5 "clean vs impaired link"; §10 "5 % loss via netem").
# Owner: Huzaifa Hanif (CS-24039). Issue #18.
#
# Usage: scripts/netem.sh apply IFACE loss [PCT]   # default PCT 5 (§10)
#        scripts/netem.sh apply IFACE delay MS      # MS required; §11 gives no value
#        scripts/netem.sh clear IFACE               # back to a clean link
#        scripts/netem.sh show IFACE
# Needs root (sudo) and iproute2. NETEM_DRY_RUN=1 prints the tc commands instead of running them.
set -euo pipefail

LOSS_DEFAULT_PCT=5 # §10: "5 % loss via netem"

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/netem.sh apply IFACE loss [PCT]   # default PCT 5 (§10), 0-100
       scripts/netem.sh apply IFACE delay MS      # MS required; §11 gives no value
       scripts/netem.sh clear IFACE               # back to a clean link
       scripts/netem.sh show IFACE
USAGE
  exit 2
}

run() {
  if [[ "${NETEM_DRY_RUN:-0}" == "1" ]]; then
    echo "$*"
  else
    "$@"
  fi
}

is_number() { [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]; }

[[ $# -ge 2 ]] || usage
cmd="$1"
iface="$2"

case "$cmd" in
  apply)
    [[ $# -ge 3 ]] || usage
    profile="$3"
    case "$profile" in
      loss)
        pct="${4:-$LOSS_DEFAULT_PCT}"
        if ! is_number "$pct" || awk -v p="$pct" 'BEGIN { exit !(p > 100) }'; then
          echo "netem.sh: bad loss percent (0-100): $pct" >&2
          exit 2
        fi
        run tc qdisc replace dev "$iface" root netem loss "${pct}%"
        ;;
      delay)
        [[ $# -ge 4 ]] || { echo "netem.sh: delay needs MS" >&2; usage; }
        ms="$4"
        is_number "$ms" || { echo "netem.sh: bad delay ms: $ms" >&2; exit 2; }
        run tc qdisc replace dev "$iface" root netem delay "${ms}ms"
        ;;
      *)
        echo "netem.sh: unknown profile: $profile" >&2
        usage
        ;;
    esac
    ;;
  clear)
    # Delete only when a netem qdisc is present; a failing tc (no sudo, bad iface) must fail
    # loudly so a "clean link" run is never silently impaired. The query always runs for real.
    current="$(tc qdisc show dev "$iface")"
    if grep -q netem <<<"$current"; then
      run tc qdisc del dev "$iface" root
    fi
    ;;
  show)
    run tc qdisc show dev "$iface"
    ;;
  *)
    usage
    ;;
esac
