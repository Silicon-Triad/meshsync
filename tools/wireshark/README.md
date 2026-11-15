# tools/wireshark

Wireshark / tshark dissector for the meshsync protocol, so captures show `MESHSYNC` as its own
protocol above TCP and UDP with every field decoded (R5; D5 brought into scope by #81).

Owner: Muhammad Ahmed Qazi (@Muhammad-Ahmed-Qazi), protocol owner; written under #82 by
Mujtaba Jawaid Rao. Related issues: #81, #82, #68 (the captures it is tested on).
Rules: [`AGENTS.md`](../../AGENTS.md).

## Use

```bash
tshark -X lua_script:tools/wireshark/meshsync.lua -r data/captures/cap2b.pcapng -Y meshsync
tools/wireshark/decode.sh data/captures/cap1.pcapng.gz      # count message types
```

Wireshark GUI: copy `meshsync.lua` to `~/.local/lib/wireshark/plugins/` and restart. Wireshark
ignores Lua scripts when run as root, so run it as a normal user.

Display filters: `meshsync`, `meshsync.type == 0x20` (CHUNK_REQ), `meshsync.req_id == 35`,
`meshsync.error.code == 4` (AUTH_FAILED), `meshsync.disc.name == "kratos"`.

## What it decodes (values from `docs/PROTOCOL.md` and `protocol/constants.py`)

- **TCP** (§4.1, registered on TCP 47001 and as a heuristic for any port): the 12-byte header
  `!2sBBII` (MAGIC "MS", VER, TYPE by name, REQ_ID, BODY_LEN), then the body: JSON bodies go to
  Wireshark's `json` dissector, CHUNK_DATA bodies are shown as raw chunk bytes, ERROR also shows
  its `code` by name (§10) and `msg`. Messages are reassembled with `dissect_tcp_pdus` from
  BODY_LEN, so a message split over segments, or several in one segment, decode correctly.
  Info column: `meshsync: CHUNK_REQ req_id=35`.
- **UDP 47000** (§3.1): ANNOUNCE / LEAVE with `name`, `peer_id`, `tcp_port`, `index_rev`, `ts`
  and `tag`. Info: `meshsync: ANNOUNCE kratos peer_id=953B75EA`.
- Expert info: bad MAGIC, unknown VER or TYPE, BODY_LEN above 1 MiB, body cut off by snaplen.
- TLS-mode traffic stays `TLSv1.3` (encrypted), as expected; R5 uses plain mode.

## Tested on

- `data/captures/cap2b.pcapng` (#68): 4 × HELLO → HELLO_ACK → ERROR `AUTH_FAILED`, plus ANNOUNCE
  and LEAVE datagrams.
- `data/captures/cap1.pcapng.gz` (#68): every type seen in a sync. These captures were taken
  with snaplen 256, so TCP cannot reassemble them: only messages that start a segment are
  decoded (e.g. the first of 8 CHUNK_REQs packed in one segment), and bodies are cut off.
- A full-size capture of two local peers on TCP 47012 / 47013 (not committed): every message
  decoded through reassembly and the heuristic, 37 CHUNK_REQ / 37 CHUNK_DATA for a 37-chunk
  file.
- `tests/unit/test_wireshark_dissector.py` runs `decode.sh` on the two committed captures; it is
  skipped without tshark (CI) or as root.
