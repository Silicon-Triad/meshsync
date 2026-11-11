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
  sed -n '6,10p' "$0" | sed 's/^# \{0,1\}//' >&2
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
        is_number "$pct" || { echo "netem.sh: bad loss percent: $pct" >&2; exit 2; }
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
    # Deleting a root qdisc that does not exist fails; a clean link is the goal either way.
    run tc qdisc del dev "$iface" root 2>/dev/null || true
    ;;
  show)
    run tc qdisc show dev "$iface"
    ;;
  *)
    usage
    ;;
esac
