#!/usr/bin/env bash
# Network baseline for §11 / R6 (issue #70): ping RTT and iperf3 throughput between every
# ordered pair of hosts, 5 runs each (§11). Run once, from one host; the others over SSH.
# Owner: Huzaifa Hanif (CS-24039); written for #70 by Mujtaba Jawaid Rao.
#
# Usage: scripts/measure/baseline.sh OUTDIR NAME:IP[:SSH_TARGET] NAME:IP[:SSH_TARGET] ...
#   A host without SSH_TARGET is this machine. Example:
#   scripts/measure/baseline.sh data/baseline/raw kratos:192.168.100.92 \
#     minipc:192.168.100.190:kratos@192.168.100.190 defalt:192.168.100.80:eemaan@192.168.100.80
# Writes OUTDIR/ping_<src>_<dst>_run<k>.txt and OUTDIR/iperf3_<src>_<dst>_run<k>.json.
# Needs ping and iperf3 on every host. Summarise with scripts/measure/baseline.py.
# Env (method settings, not protocol values): RUNS (5, §11), PINGS per run (10),
# IPERF_SECONDS per run (10, iperf3's default).
set -euo pipefail

usage() {
  echo "usage: $0 OUTDIR NAME:IP[:SSH_TARGET] NAME:IP[:SSH_TARGET] [...]" >&2
  exit 2
}

[[ $# -ge 3 ]] || usage
out="$1"
shift
RUNS="${RUNS:-5}"
PINGS="${PINGS:-10}"
IPERF_SECONDS="${IPERF_SECONDS:-10}"
mkdir -p "$out"

names=() ips=() targets=()
for spec in "$@"; do
  IFS=: read -r name ip target <<<"$spec"
  [[ -n "$name" && -n "$ip" ]] || usage
  names+=("$name") ips+=("$ip") targets+=("${target:-}")
done

# One SSH connection per host, reused (one password prompt per host instead of one per command).
ssh_opts=(-o ControlMaster=auto -o "ControlPath=$HOME/.ssh/cm-baseline-%r@%h:%p" -o ControlPersist=15m)

on() { # on TARGET CMD...: run CMD on this machine (empty TARGET) or over SSH
  local target="$1"
  shift
  if [[ -z "$target" ]]; then
    "$@"
  else
    ssh "${ssh_opts[@]}" "$target" "$(printf '%q ' "$@")"
  fi
}

n="${#names[@]}"
for ((s = 0; s < n; s++)); do
  for ((d = 0; d < n; d++)); do
    [[ "$s" -ne "$d" ]] || continue
    src="${names[$s]}" dst="${names[$d]}"
    for ((k = 1; k <= RUNS; k++)); do
      echo "ping $src -> $dst run $k/$RUNS" >&2
      on "${targets[$s]}" ping -c "$PINGS" "${ips[$d]}" >"$out/ping_${src}_${dst}_run${k}.txt"
    done
    # iperf3: one-off server on dst (-1 exits after one test, -D runs it in the background).
    for ((k = 1; k <= RUNS; k++)); do
      echo "iperf3 $src -> $dst run $k/$RUNS" >&2
      on "${targets[$d]}" iperf3 -s -1 -D
      sleep 1
      on "${targets[$s]}" iperf3 -c "${ips[$d]}" -t "$IPERF_SECONDS" -J \
        >"$out/iperf3_${src}_${dst}_run${k}.json"
    done
  done
done
echo "done: $out (summarise: scripts/measure/baseline.py $out)" >&2
