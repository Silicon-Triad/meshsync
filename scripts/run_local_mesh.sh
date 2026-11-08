#!/usr/bin/env bash
# Start N meshsync peers on one machine for local testing (PROJECT_CONTEXT.md §14). Issue #7.
# Usage: scripts/run_local_mesh.sh [-n 3] [--tls] [--clean] [--base-dir ./.localmesh]
# Owner: Muhammad Ahmed Qazi (CS-24045)
set -euo pipefail

usage() { echo "usage: $0 [-n 3] [--tls] [--clean] [--base-dir ./.localmesh]" >&2; }

# --- Arguments --------------------------------------------------------------------------------
n=3
tls=0
clean=0
base=./.localmesh
while [ "$#" -gt 0 ]; do
    case "$1" in
        -n) n=${2:?missing value for -n}; shift 2 ;;
        --tls) tls=1; shift ;;
        --clean) clean=1; shift ;;
        --base-dir) base=${2:?missing value for --base-dir}; shift 2 ;;
        -h | --help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
if ! [[ "$n" =~ ^[1-9][0-9]*$ ]]; then
    echo "error: -n must be a positive integer, got '$n'" >&2
    exit 2
fi
command -v meshsync >/dev/null || { echo "error: meshsync not on PATH (activate .venv)" >&2; exit 1; }

# --- Paths and ports --------------------------------------------------------------------------
repo=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)
base=$(realpath -m "$base")
# §14: TCP 47001..4700N per peer, one shared UDP discovery port 47000 (SO_REUSEADDR).
udp_port=47000
tcp_base=47000

# --- --clean: delete only <repo>/.../.localmesh, never anything else --------------------------
if [ "$clean" -eq 1 ]; then
    if [[ "$base" != "$repo"/* || "$(basename "$base")" != .localmesh ]]; then
        echo "error: --clean only deletes a .localmesh dir inside $repo (got $base)" >&2
        exit 2
    fi
    rm -rf -- "$base"
fi
mkdir -p "$base"

# --- Shared secret: 32 random bytes as hex, the format net/auth.py reads (D4) -----------------
if [ ! -f "$base/key" ]; then
    (umask 077 && python -c 'import secrets; from meshsync.net.auth import SECRET_BYTES
print(secrets.token_hex(SECRET_BYTES))' >"$base/key")
fi

# --- TLS certs (D9): one dir holds every peer's key; fine for local tests only ------------------
names=()
for k in $(seq 1 "$n"); do names+=("peer$k"); done
if [ "$tls" -eq 1 ]; then
    if [ ! -f "$base/certs/ca.crt" ]; then
        "$repo/scripts/gen_certs.sh" "$base/certs" "${names[@]}" >/dev/null
    fi
    mode=(--tls --cert-dir "$base/certs")
else
    mode=(--plain)
fi

# --- Start peers in the background ------------------------------------------------------------
declare -A name_of  # pid -> peer name
pids=()
for k in $(seq 1 "$n"); do
    dir="$base/peer$k"
    mkdir -p "$dir/data"
    # Static peers: every other local port (fallback when broadcast does not loop back, §14).
    statics=()
    for j in $(seq 1 "$n"); do
        if [ "$j" -ne "$k" ]; then statics+=(--peer "127.0.0.1:$((tcp_base + j))"); fi
    done
    meshsync run --dir "$dir/data" --name "peer$k" --secret-file "$base/key" \
        --tcp-port "$((tcp_base + k))" --udp-port "$udp_port" "${mode[@]}" "${statics[@]}" \
        >"$dir/peer.log" 2>&1 &
    pids+=("$!")
    name_of[$!]="peer$k"
done

# --- Table of what is running -----------------------------------------------------------------
printf '%-8s %-6s %-40s %s\n' NAME PORT DIR LOG
for k in $(seq 1 "$n"); do
    printf '%-8s %-6s %-40s %s\n' "peer$k" "$((tcp_base + k))" "$base/peer$k/data" \
        "$base/peer$k/peer.log"
done

# --- Shutdown: kill every peer and wait for it ------------------------------------------------
stop_all() {
    kill "${pids[@]}" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap 'stop_all; echo stopped; exit 130' INT
trap 'stop_all; echo stopped; exit 143' TERM

# --- Wait: peers should run until Ctrl-C; any exit stops the mesh -----------------------------
rc=0
wait -n -p done_pid "${pids[@]}" || rc=$?
peer=${name_of[$done_pid]}
echo
echo "$peer exited with code $rc; last 20 lines of $base/$peer/peer.log:" >&2
tail -n 20 "$base/$peer/peer.log" >&2
stop_all
echo stopped
exit $((rc == 0 ? 1 : rc))
