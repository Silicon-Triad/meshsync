# data/captures: Wireshark captures for R5 and R7

Packet captures of meshsync on the link between two hosts, in **plain mode** (the approved R5
deviation, §2), plus one capture per §10 failure scenario marked "capture". Issue #68.

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01); captured by Mujtaba Jawaid Rao.
Related issues: #68, #82 (Lua dissector: re-open these files with it), #75 (report R5/R7).
Rules: [`AGENTS.md`](../../AGENTS.md).

Session date: 2026-11-14, demo network (`data/deploy/README.md`). Hosts: `kratos`
(192.168.100.92, sender) and `defalt` (192.168.100.80, receiver), both on Wi-Fi; `minipc` was
not running meshsync. Code: `main` at `182bca0`.

## How they were taken

On `kratos`: `sudo tshark -i wlp2s0 -s 256 -f "udp port 47000 or tcp port 47001" -w capN.pcapng`

- **Ports** from `protocol/constants.py`: UDP 47000 discovery, TCP 47001 data.
- **Snaplen 256**: only the first 256 bytes of each packet are stored. Every header (link, IP,
  TCP, our 12-byte header, the start of each JSON body) is kept; file contents are not, which
  keeps the files small and free of payload data. The real length of every packet is still
  recorded (`frame.len`), so byte counts below are exact. Wireshark cannot reassemble full
  chunk bodies from these files; R5 does not need it.
- **Privacy**: MAC addresses were replaced before commit by `scripts/measure/anon_mac.py`
  (`02:00:00:00:00:01` / `:02`, broadcast kept; mapping differs per file: in `cap1` `:02` is
  kratos, in the others `:01` is kratos; follow the IPs). The `defalt` hostname and Linux user
  name were replaced in the logs. Checked: no real MAC in any file.
- Logs: `capN-kratos.log`, `capN-defalt.log` (`--log-level INFO`). Log times: `defalt` in UTC,
  `kratos` 5 h ahead (time zone).
- Wireshark opens `.pcapng.gz` directly.

| File | Scenario (§10) | Packets | Shows |
| ---- | -------------- | ------- | ----- |
| `cap1.pcapng.gz` | **R5**: set-A sync (100 MiB) kratos → defalt | 89,677 | full message flow, the annotated packets below |
| `cap2.pcapng` | wrong secret, discovery only | 14 | ANNOUNCEs dropped as `bad tag` (D11); no TCP at all |
| `cap2b.pcapng` | wrong secret, `defalt --peer 192.168.100.92:47001` | 59 | HELLO → HELLO_ACK → **ERROR 0x04 AUTH_FAILED** → FIN, every 5 s |
| `cap3.pcapng` | untrusted cert (TLS) | 59 | ClientHello → ServerHello → **TLS Alert: Fatal, Unknown CA** |
| `cap4.pcapng.gz` | peer killed mid-transfer (`kill -9`), then restarted | 89,627 | FIN + RSTs from the killed peer; reconnect; resumed fetch |
| `cap5.pcapng` | 5 % loss (`scripts/netem.sh apply wlp2s0 loss 5` on kratos) | 12,417 | TCP retransmissions, duplicate ACKs; 10 MiB file |

## R5: message flow in `cap1` (frame numbers)

`#11` HELLO (defalt → kratos) · `#13` HELLO_ACK · `#15` AUTH · `#16` AUTH_OK · `#17` INDEX_REQ ·
`#19` INDEX · `#22`/`#23` PING/PONG · `#111` NOTIFY (`large.bin`, body 95,594 B with all chunk
hashes) · `#223` CHUNK_REQ ×8 in one segment (704 B = 8 × 88; `MAX_OUTSTANDING_PER_PEER` = 8) ·
`#225` first CHUNK_DATA · `#89667` BYE (`{"reason": "shutdown"}`). This is the legal order of
`PROTOCOL.md` §5–§6: handshake, then SYNCED, then requests and replies matched by REQ_ID.

### Annotated packet: CHUNK_REQ, `cap1` frame 317 (154 bytes on the wire)

| Layer | Bytes | Fields |
| ----- | ----- | ------ |
| Link (Ethernet II, as Linux presents the Wi-Fi frame) | 14 | dst `02:00:00:00:00:02` (kratos), src `02:00:00:00:00:01` (defalt), EtherType `0x0800` IPv4 |
| Network (IPv4) | 20 | src 192.168.100.80 → dst 192.168.100.92, protocol 6 (TCP), DF set, TTL 64 |
| Transport (TCP) | 32 | src port 48224 → dst port 47001, ACK+PSH, 20-byte header + 12 bytes of options (NOP, NOP, timestamps) |
| meshsync header (`PROTOCOL.md` §4.1, `!2sBBII`) | 12 | `4d 53` MAGIC "MS" · `01` VER 1 · `20` TYPE CHUNK_REQ · `00 00 00 23` REQ_ID 35 · `00 00 00 4c` BODY_LEN 76 |
| meshsync body (JSON) | 76 | `{"hash": "b31631fd…6b0b"}`: the SHA-256 of the requested chunk |

### Annotated packet: CHUNK_DATA, `cap1` frame 225 (1,514 bytes on the wire, first segment)

Link 14 B (kratos → defalt) · IPv4 20 B (192.168.100.92 → .80, total length 1500) · TCP 32 B
(47001 → 48224, 1,448 bytes of payload in this segment) · meshsync header 12 B:
`4d 53 01 21 00 00 00 13 00 01 00 16` = "MS", VER 1, TYPE 0x21 CHUNK_DATA, **REQ_ID 19** (the
reply to the CHUNK_REQ with REQ_ID 19 in frame 223), BODY_LEN 65,558 · body: raw chunk bytes
(the remaining 64 KiB arrive in the following ~45 segments, which TCP reassembles).

## Header and body overhead (`cap1`, compactness vs readability, §12)

| Quantity | Bytes |
| -------- | ----- |
| File (`large.bin`) | 104,857,600 |
| TCP payload kratos → defalt (all meshsync messages) | 105,081,160 (+223,560 = **+0.21 %**) |
| … of which our 12-byte headers on the 1,403 CHUNK_DATA messages | 16,836 (**0.016 %**) |
| … of which the one NOTIFY body (JSON, all 1,403 chunk hashes) | 95,594 |
| Frames kratos → defalt (Ethernet + IP + TCP headers added) | 109,889,544 (+4.58 % over the TCP payload) |
| Frames defalt → kratos (ACKs, CHUNK_REQs: 1,403 × 88 B of meshsync payload) | 1,237,193 |
| **All frames, both directions** | **111,126,737 = file + 5.98 %** |

The binary 12-byte header is negligible for bulk data; JSON control messages (here mostly the
NOTIFY's hash list) are the visible meshsync overhead, and TCP/IP headers (66 B per 1,514-B
frame) dominate overall. The 802.11 MAC/PHY overhead is not visible in a managed-mode capture
(Linux presents frames as Ethernet), so true air-time overhead is higher; a limitation.

## R7: what each failure capture shows

- **Wrong secret (`cap2`, `cap2b`).** Two layers reject it. In `cap2` the ANNOUNCEs (one every
  5 s, `ANNOUNCE_INTERVAL`) carry an HMAC tag made with the secret (D11); both sides drop them as
  `bad tag`, so no TCP connection is ever attempted. Reaching the TCP handshake needs
  `--peer` (static peer, §14), skipping discovery: `cap2b` shows HELLO (`#6`) → HELLO_ACK (`#8`)
  → **ERROR `{"code": 4, "msg": "AUTH_FAILED: bad mac_r"}`** (`#10`) → FIN from both sides, and
  the same every 5 s. The initiator detects it first, on the responder's `mac_r`. Only nonces and
  HMACs cross the wire, never the secret (D4).
- **Untrusted certificate (`cap3`).** `defalt` used a certificate from a different CA (made with
  `scripts/gen_certs.sh` on kratos, then copied: see Problems). Client Hello (`#7`) → Server
  Hello (`#9`) → **Alert, Level Fatal, Description Unknown CA** (`#14`, from `defalt`, which
  rejects kratos's certificate). No meshsync message is ever sent; repeats every 5 s.
- **Peer killed mid-transfer (`cap4`).** At t ≈ 91.0 s `defalt`'s process is killed (`kill -9`):
  its kernel closes the socket with FIN and then RSTs (frames `#48679`–), kratos logs
  `peer down ... peer closed connection` at once and discovery expires the peer 11 s later.
  After the restart (data resumes at t ≈ 117.8 s) `defalt` fetched **633 of 1,403 chunks**: the
  770 stored before the kill were kept, and the file assembled correctly (logs).
- **5 % loss (`cap5`).** 7,935 data segments from kratos, **402 retransmissions (5.1 %)**, 52 fast
  retransmissions, 715 duplicate ACKs. The 10 MiB file arrived intact (144 chunks), at about
  1.5 Mbit/s against about 7.9 Mbit/s in `cap1`: TCP keeps the data correct and pays in time
  (§12 reliability vs latency).

## Observations

- **`replay ts` drops on `defalt`** (`cap1`, `cap4` logs): `defalt` dropped some of kratos's
  ANNOUNCEs as replays. In `cap1` (taken on kratos) kratos's ANNOUNCE timestamps strictly
  increase, one per 5 s, so kratos did not send duplicates; the receiver most likely got the same
  broadcast twice over Wi-Fi. The replay check (decision (b) in `net/discovery.py`) dropped the
  copy, as designed; harmless.
- **Discovery expired kratos during the loss run** (`cap5-defalt.log`) while the TCP connection
  kept working. Wi-Fi broadcast frames are not acknowledged or retransmitted, so under loss and a
  busy link several ANNOUNCEs in a row can miss `PEER_EXPIRY` (15 s). Discovery and the TCP
  connection judge liveness separately; the open connection was unaffected.
- `cap1` (no netem) still has 83 TCP retransmissions: normal Wi-Fi loss.
- The meshsync transfer in `cap1` ran at about 7.9 Mbit/s (106 s for 100 MiB), about half of the
  iperf3 ceiling for kratos → defalt (16.6 Mbit/s, `data/baseline/`, #70).

## Problems and fixes

| Problem | Fix |
| ------- | --- |
| Wrong secret gave no ERROR 0x04, only `bad tag` (D11 stops it at discovery) | kept as `cap2`; `cap2b` uses `--peer` to reach the TCP handshake |
| `scripts/gen_certs.sh` failed on `defalt` (OpenSSL 1.1.1f, Ubuntu 20.04): `defalt.crt: verification failed` | made the untrusted CA on `kratos` (works there) and copied it; deleted afterwards |
| MAC addresses in every frame | `scripts/measure/anon_mac.py` before commit |
| `kratos`'s branch tracked `main`, so `git push` refused | pushed with `git push -u origin exp/68-captures` (never to `main`) |
