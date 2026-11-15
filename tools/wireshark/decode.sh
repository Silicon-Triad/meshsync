#!/usr/bin/env bash
# Decode a capture with the meshsync dissector and count the message types (#82).
# Usage: tools/wireshark/decode.sh CAPTURE.pcapng[.gz] [extra tshark args...]
#   e.g. tools/wireshark/decode.sh data/captures/cap2b.pcapng
# Prints "<count> <TYPE>" per meshsync message type and discovery datagram.
# Exits 0 without output (and a note on stderr) when tshark is not installed.
# Wireshark ignores Lua scripts when run as root: run this as a normal user.
# Owner: Muhammad Ahmed Qazi (CS-24045); written for #82 by Mujtaba Jawaid Rao.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 CAPTURE.pcapng[.gz] [extra tshark args...]" >&2
  exit 2
fi
if ! command -v tshark >/dev/null; then
  echo "decode.sh: tshark not installed; skipping" >&2
  exit 0
fi
cap="$1"
shift
lua="$(cd "$(dirname "$0")" && pwd)/meshsync.lua"

tshark -X "lua_script:$lua" -r "$cap" "$@" -Y "meshsync" \
  -T fields -e meshsync.type -e meshsync.disc.type 2>/dev/null |
  tr '\t,' '\n\n' | sed '/^$/d' | sort | uniq -c |
  awk 'BEGIN {
         n["0x01"]="HELLO"; n["0x02"]="HELLO_ACK"; n["0x03"]="AUTH"; n["0x04"]="AUTH_OK";
         n["0x10"]="INDEX_REQ"; n["0x11"]="INDEX"; n["0x12"]="NOTIFY";
         n["0x20"]="CHUNK_REQ"; n["0x21"]="CHUNK_DATA"; n["0x30"]="PING"; n["0x31"]="PONG";
         n["0x7e"]="ERROR"; n["0x7f"]="BYE" }
       { name = ($2 in n) ? n[$2] : $2; print $1, name }'
