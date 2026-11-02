# Project Context — P2P File Synchronisation System (CS-327 CEP)

> Single source of truth for the project. Add to the Claude Project context and commit to the repo root as `docs/PROJECT_CONTEXT.md`.
> **Status:** Proposal approved (Nov 2026). Implementation starting.
> **Working name:** `meshsync` (package/CLI name; final name still open — see §17).

---

## 1. Summary

Three Linux laptops each hold a shared folder. Any create / edit / delete on one host propagates to the others within seconds, with **no central server**. Every host runs the same program as an equal peer.

Core features:

- Own application layer protocol on raw sockets (UDP discovery, TCP everything else).
- Chunk-level delta sync: only missing chunks travel, verified by SHA-256.
- Multi-source download: chunks pulled from several peers in parallel.
- Version-vector conflict detection: concurrent edits keep both copies.
- HMAC challenge–response authentication + TLS 1.3 encryption mode.
- Improvements over existing protocols (FTP, HTTP, rsync, BitTorrent, mDNS) — instructor requirement (§5).

---

## 2. Course Requirements (from the brief and rubric)

| Req  | Requirement                                                  | How we meet it                                               |
| ---- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| R1   | Original application protocol: message types, formats, legal order, actions on receipt, errors | §6 spec; `docs/PROTOCOL.md` in repo                          |
| R2   | Implement directly on socket API; no application-protocol libraries | Python `socket` only; `hashlib`/`hmac`/`ssl`/`json`/`struct` are helpers |
| R3   | ≥ 3 independent hosts, distinct IPs                          | 3 laptops on hotspot / lab switch; `ip addr` + network diagram |
| R4   | Justify transport against reliability, throughput, timing, security | UDP discovery, TCP data (§6.1)                               |
| R5   | Wireshark capture, annotated layer by layer                  | Plain mode capture of `CHUNK_REQ`/`CHUNK_DATA`               |
| R6   | Measure RTT, throughput, representative op; ≥ 2 conditions; repeated runs | §11; 5 runs each, mean ± std dev                             |
| R7   | Deliberate behaviour under failure                           | §10 scenarios                                                |
| §6   | Design tensions                                              | §12                                                          |
| §7   | Sustainability analysis                                      | §13                                                          |
| —    | Instructor: improvements inspired by lacks in existing protocols | §5                                                           |
| —    | AI use declared in appendix                                  | `docs/AI_USE_LOG.md` (§16)                                   |
| —    | Every member can explain any part at demo                    | Cross-review all PRs                                         |

Approved deviations (stated in proposal): custom option (extends Option E); R5 capture in plain mode; no client-server vs P2P scaling comparison.

---

## 3. Team and Ownership

| Member              | Roll No. | Implementation                                               | Report sections                                     |
| ------------------- | -------- | ------------------------------------------------------------ | --------------------------------------------------- |
| Muhammad Ahmed Qazi | CS-24045 | Discovery, connection manager, framing, auth, TLS, state machine | Protocol spec (R1), transport (R4)                  |
| Mujtaba Jawaid Rao  | CS-24047 | Scanner, index, chunking, sync engine, version vectors, conflicts, tombstones | Architecture/deployment (R3), failure (R7)          |
| Huzaifa Hanif       | CS-24039 | Transfer manager (multi-source, pipelining, scheduling), metrics, measurement scripts | Measurement (R6), capture (R5), sustainability (§7) |
| All                 | —        | Integration, 3-host testing, demo                            | Design tensions, intro, final edit                  |

Group: G2.

---

## 4. Design Decisions (locked)

| #    | Decision                                                     |
| ---- | ------------------------------------------------------------ |
| D1   | Python 3.11+, `threading` + blocking `socket`. No asyncio, no networking frameworks. |
| D2   | Version vectors for conflict detection. Never compare clocks across hosts. |
| D3   | Framing: fixed 12-byte binary header + JSON control body / raw bytes for chunk data. |
| D4   | HMAC-SHA256 challenge–response with shared secret.           |
| D5   | Wireshark Lua dissector = future work.                       |
| D6   | Pure P2P, full mesh. No server mode.                         |
| D7   | Deployment on 3 Linux laptops.                               |
| D8   | Local index = JSON file, atomic write (temp + `os.replace`). |
| D9   | TLS 1.3 mode (`--tls`, mutual certs from group CA) is a core requirement; `--plain` kept for R5 and baseline. |
| D10  | **Content-defined chunking** (Gear rolling hash, FastCDC-style): min 16 KiB, avg 64 KiB, max 256 KiB. Fixed-size chunking kept as `--chunking fixed` for comparison. *(Proposed as improvement; confirm — §17.)* |
| D11  | **Authenticated discovery**: ANNOUNCE carries HMAC tag + timestamp. *(Proposed as improvement; confirm — §17.)* |
| D12  | Chunk scheduling: rarest-first across peers, round-robin tiebreak. |
| D13  | Change detection by polling (2 s), not inotify.              |
| D14  | Renames = delete + create. Subdirectories supported.         |
| D15  | Runtime uses **standard library only**. Dev/analysis deps (pytest, ruff, pandas, matplotlib) are separate. |

---

## 5. Improvements Over Existing Protocols (instructor requirement)

| Protocol                                        | Lack                                                         | Our improvement                                              |
| ----------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| FTP                                             | Plaintext credentials; separate control/data connections (NAT/firewall issues); whole-file resend; no integrity check | HMAC auth (secret never on wire); TLS mode; single multiplexed TCP connection; chunk delta; SHA-256 per chunk |
| HTTP/1.1                                        | Head-of-line blocking (pipelined responses must be in order); client-server only; verbose text headers | `REQ_ID` → out-of-order responses on one connection; P2P; 12-byte binary header |
| rsync                                           | Two hosts only; needs SSH; one-shot; no conflict detection   | Multi-peer mesh; continuous sync; version vectors            |
| BitTorrent                                      | Immutable content; needs tracker/DHT; no deletes             | Mutable files; LAN discovery, no tracker; tombstones         |
| NFS / SMB                                       | Central server; unusable offline                             | Serverless; offline edits, catch-up on rejoin                |
| Fixed-block sync (rsync blocks, naive chunking) | Insert near start shifts all boundaries → everything re-sent | Content-defined chunking (D10)                               |
| mDNS / SSDP discovery                           | Unauthenticated, spoofable                                   | HMAC-tagged ANNOUNCE (D11)                                   |
| FTP REST / HTTP Range resume                    | Resume only from same server, at a byte offset               | Resume at chunk granularity from **any** peer                |
| Timestamp-based sync tools                      | Clock skew → wrong winner, silent data loss                  | Version vectors; conflict copies                             |

Each improvement should have a measurement or demo backing it (§11).

---

## 6. Protocol Specification

### 6.1 Transport

| Traffic         | Transport                     | Reason                                                       |
| --------------- | ----------------------------- | ------------------------------------------------------------ |
| ANNOUNCE, LEAVE | UDP broadcast, port **47000** | Needs broadcast (TCP can't); soft state, a lost announce is replaced in 5 s |
| All else        | TCP, port **47001**           | Files must arrive complete and in order; timing not critical; TLS runs over TCP |

### 6.2 Discovery datagram (UDP, JSON, ≤ 512 B)

```json
{"magic":"MS","ver":1,"type":"ANNOUNCE","peer_id":"A1F3C9D2","name":"ahmed",
 "tcp_port":47001,"index_rev":57,"ts":1730473200,"tag":"<hex HMAC>"}
```

- `tag = HMAC-SHA256(K, canonical_json_without_tag)`. Reject if tag bad or `|now - ts| > 30 s`.
- `LEAVE` = same shape, `type:"LEAVE"`.
- Interval 5 s; peer-table expiry 15 s.

### 6.3 TCP header (network byte order, `struct` format `!2sBBII`)

| Offset | Size | Field    | Notes                                         |
| ------ | ---- | -------- | --------------------------------------------- |
| 0      | 2    | MAGIC    | `b"MS"`                                       |
| 2      | 1    | VER      | `1`                                           |
| 3      | 1    | TYPE     | §6.4                                          |
| 4      | 4    | REQ_ID   | uint32; reply echoes request; 0 = unsolicited |
| 8      | 4    | BODY_LEN | uint32; max 1 MiB                             |
| 12     | N    | BODY     | JSON (UTF-8) or raw bytes (CHUNK_DATA)        |

### 6.4 Message types

| Code | Name       | Dir  | Body                               | Action on receipt                             |
| ---- | ---------- | ---- | ---------------------------------- | --------------------------------------------- |
| 0x01 | HELLO      | I→R  | `{peer_id,name,proto_ver,nonce_i}` | Check version → HELLO_ACK                     |
| 0x02 | HELLO_ACK  | R→I  | `{peer_id,name,nonce_r,mac_r}`     | Verify `mac_r` → AUTH                         |
| 0x03 | AUTH       | I→R  | `{mac_i}`                          | Verify → AUTH_OK, else ERROR 0x04 + close     |
| 0x04 | AUTH_OK    | R→I  | `{}`                               | → AUTHENTICATED                               |
| 0x10 | INDEX_REQ  | ↔    | `{}`                               | Reply INDEX                                   |
| 0x11 | INDEX      | ↔    | `{index_rev,files:[entry]}`        | Compare every entry                           |
| 0x12 | NOTIFY     | ↔    | `{entry}`                          | Compare one entry                             |
| 0x20 | CHUNK_REQ  | ↔    | `{hash}`                           | Reply CHUNK_DATA or ERROR 0x10                |
| 0x21 | CHUNK_DATA | ↔    | raw bytes                          | Verify SHA-256; store or re-request elsewhere |
| 0x30 | PING       | ↔    | `{t}`                              | Reply PONG                                    |
| 0x31 | PONG       | ↔    | `{t}`                              | RTT = now − t                                 |
| 0x7E | ERROR      | ↔    | `{code,msg}`                       | Log; close if fatal                           |
| 0x7F | BYE        | ↔    | `{reason}`                         | Close cleanly                                 |

I = initiator, R = responder. Dial rule: lower `peer_id` initiates.

### 6.5 Authentication

```
I → R  HELLO      nonce_i (16 random bytes, hex)
R → I  HELLO_ACK  nonce_r, mac_r = HMAC(K, "R" | nonce_i | nonce_r)
I → R  AUTH       mac_i = HMAC(K, "I" | nonce_r | nonce_i)
R → I  AUTH_OK
```

Compare MACs with `hmac.compare_digest`. Handshake timeout 10 s.

### 6.6 TLS mode

- `ssl.SSLContext(PROTOCOL_TLS_SERVER / CLIENT)`, `minimum_version = TLSv1_3`, `verify_mode = CERT_REQUIRED` both sides.
- Group CA + per-peer cert generated by `scripts/gen_certs.sh` (OpenSSL). Certs/keys **never committed**.
- Wrap socket right after `connect()`/`accept()`, before HELLO. Protocol unchanged inside.
- Mode mismatch → TLS handshake fails → logged, closed.

### 6.7 State machine

```
CONNECTED --HELLO/ACK/AUTH/AUTH_OK--> AUTHENTICATED --INDEX exchange--> SYNCED
   |                                        |                              |  (NOTIFY, CHUNK_*, PING, INDEX_REQ)
   +-- fatal ERROR / 10 s timeout ----------+---- BYE / 3 missed PONG -----+--> CLOSED
```

Any out-of-order message before AUTHENTICATED → ERROR 0x03 + close. PING every 10 s.

### 6.8 Error codes

| Code | Name            | Fatal |
| ---- | --------------- | ----- |
| 0x01 | BAD_HEADER      | Y     |
| 0x02 | BAD_VERSION     | Y     |
| 0x03 | ILLEGAL_STATE   | Y     |
| 0x04 | AUTH_FAILED     | Y     |
| 0x05 | TOO_LARGE       | Y     |
| 0x10 | CHUNK_NOT_FOUND | N     |
| 0x11 | CHUNK_CORRUPT   | N     |
| 0x20 | BUSY            | N     |
| 0x7F | INTERNAL        | N     |

---

## 7. Sync Semantics

### 7.1 Index entry

```json
{"path":"notes/report.txt","size":183204,"file_hash":"<sha256>",
 "chunks":["<sha256>", "..."],"version":{"A1F3C9D2":4,"B77C0E11":2},"deleted":false}
```

Paths are POSIX, relative to sync root, never containing `..` (reject on receipt).

### 7.2 Version-vector compare

| Result           | Action                                                       |
| ---------------- | ------------------------------------------------------------ |
| Remote dominates | Fetch                                                        |
| Local dominates  | Nothing                                                      |
| Equal            | Skip                                                         |
| Concurrent       | Conflict: higher `peer_id` keeps name; other saved as `name.conflict-<peername>.ext` on all peers; merged vector + own bump |

### 7.3 Rules

- Local edit → bump own counter → NOTIFY all peers.
- Delete → tombstone (`deleted:true`, bumped version), propagated like an edit. Tombstones kept (no GC in scope).
- Assembly: write to `<root>/.meshsync/tmp/`, verify `file_hash`, `os.replace` into place. Never expose partial files.
- Chunk store: `<root>/.meshsync/chunks/<hash[:2]>/<hash>`. `.meshsync/` excluded from scanning.
- Max 8 outstanding CHUNK_REQ per peer; on peer loss, reassign its outstanding requests.

---

## 8. Repository

### 8.1 Setup

```bash
gh repo create <owner>/meshsync --private --description "CS-327 CEP: P2P file synchronisation" --clone
cd meshsync
# add collaborators
gh api -X PUT repos/<owner>/meshsync/collaborators/<mujtaba-gh-username>
gh api -X PUT repos/<owner>/meshsync/collaborators/<huzaifa-gh-username>
```

Settings: protect `main` (require PR + 1 approval + CI pass, no force push); squash merge only; delete branch on merge.

### 8.2 Layout

```
meshsync/
├── README.md
├── pyproject.toml              # name, python>=3.11, entry point, dev extras
├── .gitignore                  # __pycache__, .venv, *.pem, *.key, *.crt, .meshsync/, data/raw/
├── .github/
│   ├── workflows/ci.yml        # ruff + pytest on push/PR
│   └── pull_request_template.md
├── docs/
│   ├── PROJECT_CONTEXT.md      # this file
│   ├── PROTOCOL.md             # full R1 spec (grows from §6)
│   ├── AI_USE_LOG.md
│   └── diagrams/
├── src/meshsync/
│   ├── __main__.py             # CLI entry: run / status / stats
│   ├── config.py               # CLI args + config file
│   ├── protocol/
│   │   ├── constants.py        # ports, type codes, error codes, limits
│   │   ├── framing.py          # pack/unpack header, read_exact, send_msg/recv_msg
│   │   └── messages.py         # body builders / validators per type
│   ├── net/
│   │   ├── discovery.py        # UDP announce/listen, peer table   (Ahmed)
│   │   ├── connection.py       # Connection: socket, state, reader thread, send lock, REQ_ID map (Ahmed)
│   │   ├── manager.py          # dial rule, accept loop, liveness    (Ahmed)
│   │   ├── auth.py             # HMAC handshake                     (Ahmed)
│   │   └── tls.py              # SSLContext builders                (Ahmed)
│   ├── sync/
│   │   ├── chunker.py          # CDC + fixed chunking               (Mujtaba)
│   │   ├── index.py            # index load/save, entries           (Mujtaba)
│   │   ├── store.py            # chunk store                        (Mujtaba)
│   │   ├── scanner.py          # polling, change detection          (Mujtaba)
│   │   ├── vclock.py           # version vector ops                 (Mujtaba)
│   │   └── engine.py           # compare, conflicts, tombstones     (Mujtaba)
│   ├── transfer/
│   │   ├── scheduler.py        # rarest-first, per-peer window      (Huzaifa)
│   │   └── assembler.py        # temp build, verify, atomic replace (Huzaifa)
│   └── metrics/
│       └── logger.py           # per-message CSV + human log        (Huzaifa)
├── tests/
│   ├── unit/                   # framing, vclock, chunker, auth, index
│   └── integration/            # multi-peer on localhost
├── scripts/
│   ├── gen_certs.sh            # group CA + per-peer certs
│   ├── run_local_mesh.sh       # 3 peers on one machine
│   ├── netem.sh                # apply/clear tc netem profiles
│   ├── gen_testdata.py         # fixed-size random files
│   └── measure/                # experiment runners
├── analysis/                   # notebooks/scripts → graphs
├── data/                       # results CSV (raw/ gitignored if large)
└── report/                     # report source + figures
```

### 8.3 Module contracts (agree before coding)

| Interface                                                    | Owner → consumer                    |
| ------------------------------------------------------------ | ----------------------------------- |
| `framing.send_msg(sock, type, req_id, body: bytes)` / `recv_msg(sock) -> (type, req_id, body)` | Ahmed → all                         |
| `Connection.request(type, body) -> Future` and handler registry `on(type, fn)` | Ahmed → Huzaifa, Mujtaba            |
| `PeerManager.peers() -> list[PeerInfo]`, events `peer_up/peer_down` | Ahmed → Mujtaba, Huzaifa            |
| `Index.get/put/entries()`, `ChunkStore.has/get/put(hash)`    | Mujtaba → Huzaifa                   |
| `Engine.on_remote_entry(peer, entry) -> Action(fetch/skip/conflict/delete)` | Mujtaba → Huzaifa                   |
| `Scheduler.fetch(entry, peers) -> completes or raises`       | Huzaifa → Mujtaba                   |
| `metrics.record(direction, peer, type, size, t)`             | Huzaifa → Ahmed (called in framing) |

---

## 9. Conventions

### 9.1 Code

- Python 3.11+, type hints everywhere, `dataclasses` for records.
- `ruff` (lint + format), line length 100. `pytest` for tests.
- `logging` module, not `print`. Logger per module.
- Threads: one reader thread per connection; all sends through `Connection.send` with a lock; shared state guarded by locks; no blocking calls while holding a lock.
- Every socket has a timeout. Use `read_exact` (loop until N bytes) — never assume `recv` returns full messages.
- Constants only in `protocol/constants.py`.
- Never commit secrets, certs, keys, or personal test folders.

### 9.2 Git

- Branches: `feat/<area>-<short>`, `fix/...`, `docs/...`, `exp/...` (measurements).
- Commits: Conventional Commits — `feat(net): add HMAC handshake`.
- PR: small, one concern; template lists what changed, how tested, protocol impact. **Reviewer = different member** (supports demo-explainability rule).
- Protocol changes: update `docs/PROTOCOL.md` in the same PR.
- Issues + GitHub Project board: Backlog / In progress / Review / Done; labels `net`, `sync`, `transfer`, `metrics`, `report`, `bug`.

### 9.3 CI (`.github/workflows/ci.yml`)

Ubuntu, Python 3.11 + 3.12: `pip install -e .[dev]`, `ruff check`, `ruff format --check`, `pytest -q`.

---

## 10. Failure Scenarios (R7)

| Scenario                           | Expected                                                     | Evidence             |
| ---------------------------------- | ------------------------------------------------------------ | -------------------- |
| Peer killed mid-transfer           | Outstanding chunks reassigned; file completes from other peer | Log + capture        |
| Concurrent edits on 2 hosts        | Conflict copy on all peers                                   | Folder listing + log |
| Peer offline, files change, rejoin | Catches up via index exchange                                | Log timeline         |
| Wrong secret                       | ERROR 0x04, closed                                           | Log + capture        |
| Spoofed / replayed ANNOUNCE        | Dropped (bad tag / stale ts)                                 | Log                  |
| Untrusted cert / mode mismatch     | TLS alert, closed                                            | Log + capture        |
| Corrupt chunk (test flag)          | Hash mismatch → re-request elsewhere                         | Log                  |
| 5 % loss via netem                 | Completes, slower; TCP retransmits visible                   | Capture + timings    |

---

## 11. Measurement Plan (R6)

Metrics: RTT (PING/PONG, cross-checked with `ping`), application throughput (payload bytes ÷ time, vs `iperf3` ceiling), time to sync a 100 MB file, bytes on wire.

Conditions (5 runs each, mean ± std dev, table + graph):

1. Full-file vs delta sync after small edit.
2. **Fixed vs content-defined chunking** after an insert near file start (backs D10).
3. Single-source vs two-source download.
4. Chunk size 16 / 64 / 256 KiB.
5. Clean vs impaired link (`tc netem` delay / loss).
6. Plain vs TLS: setup time and throughput.
7. (Optional) Wi-Fi vs wired.

Results interpreted via d_nodal = d_proc + d_queue + d_trans + d_prop.
Raw output: `data/<experiment>/<run>.csv`; graphs from `analysis/`.

---

## 12. Design Tensions (brief §6)

| Tension                       | Position                             | Evidence                            |
| ----------------------------- | ------------------------------------ | ----------------------------------- |
| Reliability vs latency        | TCP data, UDP discovery              | Loss runs                           |
| State vs simplicity           | Persistent index + version vectors   | Rejoin correctness, complexity cost |
| Compactness vs readability    | Binary header + JSON control         | Header/body byte overhead           |
| Connection reuse vs resources | Persistent full mesh; O(n²) at scale | Reuse vs reconnect latency          |
| Security vs performance       | HMAC + TLS 1.3                       | Plain vs TLS                        |

---

## 13. Sustainability (brief §7)

Measure bytes for small edit: delta vs full copy; CDC vs fixed after insert. Project to e.g. 10,000 users × 20 edits/day → GB/day, GB/year → energy using a cited kWh/GB figure with uncertainty. Note hashing CPU cost as counter-weight.

---

## 14. Running

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e .[dev]
meshsync run --dir ~/MeshSync --name ahmed --secret-file ~/.meshsync/key --tls --cert-dir ~/.meshsync/certs
meshsync status
meshsync stats
```

Local testing (one machine): `scripts/run_local_mesh.sh` starts 3 peers with separate `--dir` and `--tcp-port` (47001/47002/47003); UDP discovery shares 47000 via `SO_REUSEADDR`. Fallback: `--peer 127.0.0.1:47002` static list.

Network check before 3-host tests: same subnet, `ping` each other, firewall allows UDP 47000 + TCP 4700x (`sudo ufw allow ...` if active). Some hotspots block client-to-client traffic — test early.

---

## 15. Milestones

| Week | Milestone                                                    |
| ---- | ------------------------------------------------------------ |
| 10   | Repo, CI, constants, framing + tests; discovery + handshake across 3 laptops |
| 11   | Index, CDC chunker, chunk transfer; first end-to-end sync; deletes |
| 12   | Conflicts, multi-source, failure handling, TLS; measurements + captures |
| 13   | Report, graphs, demo rehearsal, submission                   |

Adjust against the actual course calendar.

---

## 16. AI Use

Brief requires an AI-use appendix. Log in `docs/AI_USE_LOG.md`: date, member, tool, what for, what was kept/changed. Everyone must understand any AI-assisted code before merging.

---

## 17. Open Items

- [ ] Final project name (`meshsync` vs `meshfer` vs other).
- [ ] Confirm D10 (CDC) and D11 (authenticated discovery) as in-scope improvements.
- [ ] GitHub usernames for collaborators.
- [ ] Network: whose hotspot / lab switch; does it allow client-to-client traffic? USB-Ethernet adapters for wired runs?
- [ ] Test data set definition (sizes, file types).
- [ ] Map milestones to actual submission/demo dates.

---

## 18. Out of Scope / Future Work

Wireshark Lua dissector · NAT traversal / multi-subnet · inotify · tombstone garbage collection · GUI · rename detection.
