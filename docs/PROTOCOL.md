<!-- FROZEN INTERFACE (AGENTS.md §6): any change or addition needs an issue and approval from all three members first (R-6.2). -->

# meshsync Protocol Specification (version 1)

This document specifies the meshsync protocol in enough detail for another group to build a peer
that works with ours, using this file alone (R1). It is derived from `docs/PROJECT_CONTEXT.md`
§4–§7 and checked against the code on `main`. `PROJECT_CONTEXT.md` remains the source of truth
(AGENTS.md R-1.2). Where the code differs from it, the difference was raised in issue #8, and
this file describes the code's behaviour only as a decision **proposed for approval there**
(marked *proposed* in §13). Until it is approved, `PROJECT_CONTEXT.md` takes precedence. Any
protocol change must update this file in the same PR.

Constant names in `code` (e.g. `TCP_PORT`) refer to `src/meshsync/protocol/constants.py`, the
only place these values are defined.

Contents: 1 Overview · 2 Transport · 3 Discovery · 4 Framing · 5 Messages · 6 Connection setup ·
7 State machine · 8 Peer management · 9 Sync semantics · 10 Errors · 11 Security ·
12 Improvements over existing protocols · 13 Decisions and changelog

---

## 1. Overview

meshsync keeps one shared folder identical on several hosts on a LAN. There is no server
(D6): every peer runs the same program, finds the others by UDP broadcast, opens one TCP
connection to each of them (a full mesh), proves that it knows the group secret, exchanges file
indexes, and then fetches missing file pieces (chunks) from any peer that has them.

**Roles.** All peers are equal. On each connection the side that dialled is the **initiator
(I)** and the side that accepted is the **responder (R)**. Roles matter only for the handshake
(§6) and for REQ_ID numbering (§4.3).

**Terminology**

| Term          | Meaning |
| ------------- | ------- |
| `K`           | The group's shared secret: 32 bytes, stored as 64 hex characters in a file passed with `--secret-file`. Never sent on the wire. |
| `peer_id`     | A peer's identity: exactly 8 **uppercase** hex characters, e.g. `A1F3C9D2` (`PEER_ID_HEX_LEN` = 8). Random, created on first run, persisted (§8.5). Compared as strings. |
| `name`        | Human-readable peer name, e.g. `ahmed`. In TLS mode it must equal the CN of the peer's certificate (§6.2). |
| index entry   | The metadata record for one file: path, size, hashes of its chunks, version vector, deleted flag (§9.1). |
| index / `index_rev` | A peer's set of index entries, and a counter that goes up every time the index changes. |
| chunk         | A piece of file content (16–256 KiB, content-defined, D10), named by the lowercase hex SHA-256 of its bytes. |
| version vector | Map `peer_id → counter` attached to each entry; used to decide which version is newer, or whether two edits were concurrent (D2, §9.3). |
| tombstone     | An index entry with `"deleted": true`, recording a deletion (§9.5). |
| `REQ_ID`      | 32-bit number in each TCP header that pairs a reply with its request (§4.3). |
| fatal error   | An error after which the connection is closed (§10). |

**Byte order and text.** All multi-byte integers on the wire are unsigned, network byte order
(big-endian). All JSON is UTF-8. Hex case is fixed (decision (d)): `peer_id` is uppercase; every
other hex value (hashes, nonces, MACs, tags) is lowercase. Values in the other case are invalid.

---

## 2. Transport (R4)

| Traffic          | Transport                      | Port                       |
| ---------------- | ------------------------------ | -------------------------- |
| ANNOUNCE, LEAVE  | UDP broadcast to 255.255.255.255 | 47000 (`UDP_PORT`)       |
| Everything else  | TCP, optionally inside TLS 1.3 | 47001 (`TCP_PORT`)         |

Both ports can be changed (`--tcp-port`, `--udp-port`). The TCP port actually used is announced in the discovery datagram.

Why each transport (R4):

| Criterion   | Discovery: UDP | Data: TCP |
| ----------- | -------------- | --------- |
| Reliability | Not needed. Discovery is soft state: a lost ANNOUNCE is replaced by the next one 5 s later. | Required. Index entries and chunks must arrive complete, uncorrupted and in order; TCP retransmits and orders. SHA-256 per chunk adds an end-to-end check on top. |
| Throughput  | Tiny datagrams (≤ 512 B) every 5 s. | Bulk chunk transfer needs TCP congestion and flow control. One persistent connection per peer pair avoids repeated handshakes. |
| Timing      | Peers must appear within seconds; broadcast reaches all at once. TCP cannot broadcast. | Sync is seconds-level, not real-time, so TCP's retransmission delay is acceptable. `REQ_ID` multiplexing removes head-of-line blocking between requests on one connection. |
| Security    | Broadcast is visible to everyone, so each datagram carries an HMAC tag and timestamp (D11, §3). | HMAC challenge-response proves knowledge of `K` (§6.3); TLS 1.3 runs over TCP and adds encryption and certificate checks (§6.2). |

---

## 3. Discovery (UDP)

### 3.1 Datagram

One UDP datagram = one JSON object, UTF-8, at most **512 bytes** (`MAX_DATAGRAM`). Example
(the real output of §3.3):

```json
{"index_rev":57,"magic":"MS","name":"ahmed","peer_id":"A1F3C9D2","tag":"98ae7b37329977adb6fdf7b8d8eea0a046dbd848b505bb65c4a359aadd307720","tcp_port":47001,"ts":1730473200,"type":"ANNOUNCE","ver":1}
```

All nine fields are required; any other key makes the datagram invalid.

| Field       | JSON type | Constraint |
| ----------- | --------- | ---------- |
| `magic`     | string    | exactly `"MS"` (`MAGIC`) |
| `ver`       | integer   | exactly `1` (`VERSION`) |
| `type`      | string    | `"ANNOUNCE"` or `"LEAVE"` |
| `peer_id`   | string    | 8 uppercase hex characters |
| `name`      | string    | non-empty |
| `tcp_port`  | integer   | 1–65535; the port the sender accepts TCP on |
| `index_rev` | integer   | ≥ 0; the sender's current index revision |
| `ts`        | integer   | sender's Unix time in seconds (see 3.5) |
| `tag`       | string    | 64 lowercase hex characters: the HMAC tag (3.2) |

JSON booleans are not accepted where an integer is required (`true` is not `1`).

### 3.2 Tag computation (D11)

```
tag = lowercase_hex( HMAC-SHA256(K, canonical_json(all fields except "tag")) )
```

**Canonical JSON** is the one byte string that sender and receiver must both produce for the same
fields:

- keys sorted by Unicode code point (ascending);
- no whitespace: separators are exactly `,` and `:`;
- non-ASCII characters written as raw UTF-8, not `\uXXXX` escapes;
- Python equivalent: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.

The receiver recomputes the tag over the fields **it parsed** (minus `tag`), not over the raw
bytes it received. The datagram itself is also sent in canonical form, but receivers must not
rely on that.

### 3.3 Worked example

Produced by running `net/discovery.py` `build_announce()` with these inputs:

| Input       | Value |
| ----------- | ----- |
| `K` (hex)   | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| `peer_id`, `name` | `A1F3C9D2`, `ahmed` |
| `tcp_port`, `index_rev`, `ts` | `47001`, `57`, `1730473200` |

Canonical JSON without the tag (the HMAC input, 124 bytes):

```
{"index_rev":57,"magic":"MS","name":"ahmed","peer_id":"A1F3C9D2","tcp_port":47001,"ts":1730473200,"type":"ANNOUNCE","ver":1}
```

Tag:

```
98ae7b37329977adb6fdf7b8d8eea0a046dbd848b505bb65c4a359aadd307720
```

The full datagram is the one shown in 3.1 (197 bytes).

### 3.4 Receive rules (in order)

A receiver checks each datagram in this order and **drops** it, without any reply, at the first
rule that fails:

| # | Check | On failure |
| - | ----- | ---------- |
| 1 | Length ≤ 512 bytes; valid UTF-8 JSON; exactly the fields of 3.1 with valid types | drop (logged at DEBUG) |
| 2 | `peer_id` ≠ receiver's own `peer_id` (its own broadcast echoed back) | drop silently |
| 3 | `tag` equals the recomputed tag, compared in constant time | drop, WARNING |
| 4 | `|now − ts| ≤ 30 s` (`ANNOUNCE_MAX_SKEW`), `now` = receiver's Unix time | drop, WARNING |
| 5 | `ts` > the last accepted `ts` from this `peer_id` (replay, decision (b)) | drop, WARNING |
| 6 | `type` = `LEAVE`: remove the peer from the peer table now | — |
| 7 | `type` = `ANNOUNCE`: add the peer, or update its row (name, address, port, `index_rev`, last-seen time) | — |

- The peer's IP address is the datagram's **source IP**, never a value from the body.
- The last accepted `ts` per `peer_id` is remembered even after LEAVE or expiry, so a recorded
  old datagram cannot bring a departed peer back.

### 3.5 Sending, intervals, expiry

| Rule | Value |
| ---- | ----- |
| Send ANNOUNCE | every **5 s** (`ANNOUNCE_INTERVAL`), starting at start-up |
| Send LEAVE    | once at shutdown; same fields with `"type":"LEAVE"` |
| Peer expiry   | a peer not heard from for more than **15 s** (`PEER_EXPIRY`) is removed from the peer table |
| `ts`          | `max(int(unix_time), last_sent_ts + 1)`: strictly increasing, so a LEAVE in the same second as the last ANNOUNCE is not dropped as a replay by rule 5 |

Leaving the UDP peer table does **not** close an existing TCP connection; only TCP liveness or
BYE does (§8).

---

## 4. Framing (TCP)

### 4.1 Header

Every TCP message is a 12-byte header (`HEADER_SIZE`) followed by a body. Python `struct` format
`!2sBBII` (`HEADER_FMT`).

| Offset | Size | Field      | Type        | Value / constraint |
| ------ | ---- | ---------- | ----------- | ------------------ |
| 0      | 2    | `MAGIC`    | bytes       | `4d 53` = ASCII `"MS"` (`MAGIC`) |
| 2      | 1    | `VER`      | uint8       | `1` (`VERSION`) |
| 3      | 1    | `TYPE`     | uint8       | a message code from §5 (`MsgType`) |
| 4      | 4    | `REQ_ID`   | uint32 BE   | 0–4 294 967 295 (`MAX_REQ_ID` = `0xFFFFFFFF`); see 4.3 |
| 8      | 4    | `BODY_LEN` | uint32 BE   | number of body bytes, ≤ 1 048 576 (`MAX_BODY`, 1 MiB) |
| 12     | N    | `BODY`     | bytes       | UTF-8 JSON object, except CHUNK_DATA: raw chunk bytes |

A message is read as: read exactly 12 bytes, validate the header, read exactly `BODY_LEN` bytes.
TCP is a byte stream, so a reader must loop until it has all the bytes it asked for.

### 4.2 Header validation (in order)

| Check | Error (all fatal, §10) |
| ----- | ---------------------- |
| `MAGIC` ≠ `"MS"` | `BAD_HEADER` (0x01) |
| `VER` ≠ 1 | `BAD_VERSION` (0x02) |
| `TYPE` is not a code in §5 | `BAD_HEADER` (0x01), decision (a) |
| `BODY_LEN` > 1 MiB | `TOO_LARGE` (0x05) |
| body is not valid UTF-8 JSON, or does not match its schema in §5 | `BAD_HEADER` (0x01), decision (c) |

Body JSON rules: an object with exactly the keys listed for its type (no missing, no extra);
`NaN`/`Infinity` are invalid; booleans are not integers. Senders may use any JSON whitespace (our
implementation writes `{"t": 1.5}` with a space); bodies are not canonicalised.

### 4.3 REQ_ID

- A **request** carries a non-zero `REQ_ID`; the **reply** (or an ERROR answering it) carries the
  same `REQ_ID`. Replies may arrive in any order, so several requests can be outstanding on one
  connection without head-of-line blocking.
- Both sides send requests on the same connection, so IDs are split by role (decision (h)):
  the initiator uses **odd** IDs (1, 3, 5, …), the responder **even** IDs (2, 4, 6, …).
  IDs wrap after `0xFFFFFFFF` back to the first ID and skip any ID still waiting for a reply.
- On receipt, a non-zero `REQ_ID` with the **receiver's own** parity is a reply to one of the
  receiver's requests; one with the other parity is a request from the sender.
- `REQ_ID` = **0** marks a message that is neither a request nor a reply: the handshake messages
  (§6.3), NOTIFY, BYE, and an ERROR not tied to a request.

### 4.4 Example: one PING on the wire

Produced by `protocol/framing.py` `send_msg()` with type PING, `REQ_ID` 1 and body
`{"t": 1730473200.5}` (19 bytes), read back from a socket pair:

```
offset  bytes                                            ASCII
0000    4d 53 01 30 00 00 00 01 00 00 00 13 7b 22 74 22  MS.0........{"t"
0010    3a 20 31 37 33 30 34 37 33 32 30 30 2e 35 7d     : 1730473200.5}
```

| Bytes         | Field      | Meaning |
| ------------- | ---------- | ------- |
| `4d 53`       | MAGIC      | `"MS"` |
| `01`          | VER        | 1 |
| `30`          | TYPE       | 0x30 = PING |
| `00 00 00 01` | REQ_ID     | 1 (odd: sent by the initiator) |
| `00 00 00 13` | BODY_LEN   | 19 |
| `7b … 7d`     | BODY       | `{"t": 1730473200.5}` |

---

## 5. Messages

Overview. "Dir": I→R / R→I = only that direction; ↔ = either peer. "States" refers to §7.

| Code | Name       | Dir | States | REQ_ID | Body |
| ---- | ---------- | --- | ------ | ------ | ---- |
| 0x01 | HELLO      | I→R | HANDSHAKE | 0 | `{peer_id, name, proto_ver, nonce_i}` |
| 0x02 | HELLO_ACK  | R→I | HANDSHAKE | 0 | `{peer_id, name, nonce_r, mac_r}` |
| 0x03 | AUTH       | I→R | HANDSHAKE | 0 | `{mac_i}` |
| 0x04 | AUTH_OK    | R→I | HANDSHAKE | 0 | `{}` |
| 0x10 | INDEX_REQ  | ↔   | AUTHENTICATED, SYNCED | request | `{}` |
| 0x11 | INDEX      | ↔   | AUTHENTICATED, SYNCED | reply | `{index_rev, files}` |
| 0x12 | NOTIFY     | ↔   | AUTHENTICATED, SYNCED | 0 | `{entry}` |
| 0x20 | CHUNK_REQ  | ↔   | AUTHENTICATED, SYNCED | request | `{hash}` |
| 0x21 | CHUNK_DATA | ↔   | AUTHENTICATED, SYNCED | reply | raw bytes |
| 0x30 | PING       | ↔   | AUTHENTICATED, SYNCED | request | `{t}` |
| 0x31 | PONG       | ↔   | AUTHENTICATED, SYNCED | reply | `{t}` |
| 0x7E | ERROR      | ↔   | any | reply or 0 | `{code, msg}` |
| 0x7F | BYE        | ↔   | AUTHENTICATED, SYNCED | 0 | `{reason}` |

Errors common to every message: header or body invalid → §4.2. A handshake message outside the
handshake → `ILLEGAL_STATE` (§7).

### 5.1 HELLO (0x01)

Initiator's first message after the TCP (and TLS) connection is up.

| Field       | Type    | Constraint |
| ----------- | ------- | ---------- |
| `peer_id`   | string  | initiator's `peer_id`, 8 uppercase hex |
| `name`      | string  | initiator's name, non-empty |
| `proto_ver` | integer | `1` (`VERSION`) |
| `nonce_i`   | string  | 16 random bytes (`NONCE_BYTES`) as 32 lowercase hex characters |

On receipt (responder): if `proto_ver` ≠ 1 → ERROR `BAD_VERSION`, close. If `peer_id` equals
its own → ERROR `AUTH_FAILED` ("self-connection"), close. Otherwise make `nonce_r` and reply
HELLO_ACK.

### 5.2 HELLO_ACK (0x02)

| Field     | Type   | Constraint |
| --------- | ------ | ---------- |
| `peer_id` | string | responder's `peer_id` |
| `name`    | string | responder's name, non-empty |
| `nonce_r` | string | 16 random bytes, 32 lowercase hex |
| `mac_r`   | string | 64 lowercase hex; `HMAC-SHA256(K, "R" ‖ nonce_i ‖ nonce_r)` (§6.4) |

On receipt (initiator): if `peer_id` equals its own → ERROR `AUTH_FAILED`, close. Recompute
`mac_r`; if it differs → ERROR `AUTH_FAILED`, close. Otherwise send AUTH.

### 5.3 AUTH (0x03)

| Field   | Type   | Constraint |
| ------- | ------ | ---------- |
| `mac_i` | string | 64 lowercase hex; `HMAC-SHA256(K, "I" ‖ nonce_r ‖ nonce_i)` |

On receipt (responder): recompute `mac_i`; mismatch → ERROR `AUTH_FAILED`, close. Otherwise
send AUTH_OK; the connection is AUTHENTICATED.

### 5.4 AUTH_OK (0x04)

Body `{}`. On receipt (initiator): the connection is AUTHENTICATED.

### 5.5 INDEX_REQ (0x10)

Body `{}`. Asks the peer for its whole index. Each side sends one right after the connection
becomes AUTHENTICATED (§9.6). On receipt: reply INDEX with the same `REQ_ID`.

### 5.6 INDEX (0x11)

Reply to INDEX_REQ.

| Field       | Type    | Constraint |
| ----------- | ------- | ---------- |
| `index_rev` | integer | ≥ 0; sender's index revision |
| `files`     | array   | index entries (§9.1), each valid |

On receipt: compare every entry with the local one (§9.3) and act on the result. Then the
connection is SYNCED. An invalid entry makes the whole message invalid (`BAD_HEADER`, fatal).
The whole INDEX must fit in one 1 MiB body (§11.4).

### 5.7 NOTIFY (0x12)

| Field   | Type   | Constraint |
| ------- | ------ | ---------- |
| `entry` | object | one index entry (§9.1) |

Sent unsolicited (`REQ_ID` 0) to every connected peer whenever a local file is created,
changed or deleted. No reply. On receipt: compare this one entry (§9.3).

### 5.8 CHUNK_REQ (0x20)

| Field  | Type   | Constraint |
| ------ | ------ | ---------- |
| `hash` | string | 64 lowercase hex: SHA-256 of the wanted chunk |

At most **8** CHUNK_REQ outstanding per peer (`MAX_OUTSTANDING_PER_PEER`). On receipt: reply
CHUNK_DATA if the chunk is in the local store, else ERROR `CHUNK_NOT_FOUND` (0x10, non-fatal),
both with the request's `REQ_ID`.

### 5.9 CHUNK_DATA (0x21)

Body = the chunk's raw bytes (not JSON), at most 256 KiB (`CDC_MAX`), far below `MAX_BODY`.
On receipt: compute SHA-256 of the body; if it equals the requested hash, store the chunk;
otherwise discard it and request it from another peer (`CHUNK_CORRUPT` is the code for this
case in logs and metrics).

### 5.10 PING (0x30)

| Field | Type   | Constraint |
| ----- | ------ | ---------- |
| `t`   | number | sender's Unix time in seconds; integer or float |

Sent as a **request** (non-zero `REQ_ID`) every 10 s on each connection (§8.3). On receipt:
reply PONG with the same `REQ_ID` and the same `t`. Any peer must answer PING, even if it does
nothing else.

### 5.11 PONG (0x31)

Body `{t}`, echoing the PING. On receipt: the RTT is the time between sending the PING and
receiving this PONG, measured on the sender's own monotonic clock; the missed-PONG counter is
reset. `t` is informational (decision (m)).

### 5.12 ERROR (0x7E)

| Field  | Type    | Constraint |
| ------ | ------- | ---------- |
| `code` | integer | a code from §10 |
| `msg`  | string  | human-readable reason (may be empty) |

`REQ_ID` = the request it answers, or 0. On receipt: if it answers one of our requests, that
request fails with this error; otherwise log it. If the code is fatal, close the connection
(after failing the request). Never answer an ERROR with an ERROR.

### 5.13 BYE (0x7F)

| Field    | Type   | Constraint |
| -------- | ------ | ---------- |
| `reason` | string | e.g. `"shutdown"`, `"duplicate"`, `"liveness"` |

Sent with `REQ_ID` 0 just before closing on purpose. On receipt: close the connection without
replying. Outstanding requests on it fail.

---

## 6. Connection setup

### 6.1 Sequence

```
 Initiator (lower peer_id)                          Responder
 ─────────────────────────                          ─────────
 TCP connect ─────────────────────────────────────▶ accept
 [--tls only: TLS 1.3 handshake, mutual certificates, §6.2]
 HELLO     {peer_id,name,proto_ver=1,nonce_i} ────▶ check proto_ver, not self
           ◀──── {peer_id,name,nonce_r,mac_r}       HELLO_ACK
 check not self, verify mac_r
 AUTH      {mac_i} ───────────────────────────────▶ verify mac_i
           ◀──────────────────────────────── {}     AUTH_OK
 ════════════════ AUTHENTICATED (both sides) ═════════════════
 [--tls only: each side checks cert CN == peer's HELLO/HELLO_ACK name]
 INDEX_REQ ──────────▶                ◀────────── INDEX_REQ    (both directions, §9.6)
           ◀────────── INDEX          INDEX ──────────▶
 ═════════════════════ SYNCED ═══════════════════════════════
```

Any failure on the way (§6.5) ends with ERROR (if the failing side detected it) and close.

### 6.2 TLS (`--tls`, D9)

- Enabled with `--tls`, disabled with `--plain` (the default). **Both peers must use the same
  mode**; a mismatch makes the TLS handshake fail, and the connection is logged and closed.
- When: immediately after `connect()` / `accept()`, before HELLO. Everything in §5 then runs
  unchanged inside TLS, including the HMAC handshake.
- Settings, both sides: minimum version TLS 1.3; `CERT_REQUIRED` (mutual authentication: the
  server also demands a client certificate). Trust anchor: only the group CA (`ca.crt`). Each
  peer presents `<name>.crt` / `<name>.key` from `scripts/gen_certs.sh`. Certificates and keys
  are never committed.
- Hostname checking is **off**, because peers dial by the IP learned from discovery (decision
  (e)). Trust comes from the CA chain only.
- **Cert-name rule (decision (e)).** Once the HMAC handshake has succeeded, and before any other
  message is sent or accepted, each side checks that the CN of the peer's verified certificate
  equals the `name` the peer sent in HELLO / HELLO_ACK. On mismatch: ERROR `AUTH_FAILED`
  (`REQ_ID` 0), close. Without this, any member's valid certificate could claim another
  member's name.
- The TLS handshake has its own limit of 10 s (`HANDSHAKE_TIMEOUT`) per socket operation,
  separate from the HMAC handshake's overall 10 s (decision (n)).

### 6.3 HMAC challenge-response (D4)

```
I → R  HELLO      nonce_i
R → I  HELLO_ACK  nonce_r, mac_r = HMAC-SHA256(K, "R" ‖ nonce_i ‖ nonce_r)
I → R  AUTH       mac_i = HMAC-SHA256(K, "I" ‖ nonce_r ‖ nonce_i)
R → I  AUTH_OK
```

- Each nonce is 16 fresh random bytes (`NONCE_BYTES`) from a cryptographic RNG.
- `K` never crosses the wire. Each side proves knowledge of `K` by MACing the **other** side's
  fresh nonce, so a recorded handshake cannot be replayed.
- MACs are compared in constant time (`hmac.compare_digest`).
- All handshake messages, and any ERROR during the handshake, use `REQ_ID` 0.
- A side that **receives** an ERROR during the handshake closes without replying.

### 6.4 MAC input bytes (decision (g))

The HMAC input is 33 bytes: one ASCII role byte, then the two nonces as **raw bytes**
(hex-decoded), in the order shown. The MAC is sent as 64 lowercase hex characters.

| Offset | Size | `mac_r` input        | `mac_i` input        |
| ------ | ---- | -------------------- | -------------------- |
| 0      | 1    | `0x52` (`"R"`)       | `0x49` (`"I"`)       |
| 1      | 16   | `nonce_i`            | `nonce_r`            |
| 17     | 16   | `nonce_r`            | `nonce_i`            |

The different role byte makes `mac_r` ≠ `mac_i` for the same nonces, so an attacker cannot
reflect one side's MAC back as the other side's proof.

### 6.5 Worked example

Produced by `net/auth.py` `responder_mac()` and `initiator_mac()`:

| Input     | Value |
| --------- | ----- |
| `K`       | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| `nonce_i` | `00112233445566778899aabbccddeeff` |
| `nonce_r` | `ffeeddccbbaa99887766554433221100` |

```
mac_r input: 52 00112233445566778899aabbccddeeff ffeeddccbbaa99887766554433221100
mac_r:       b7acb76eb6fdec4181df88c83d284f4c9906c9225b1aacec2f82c044c758945f

mac_i input: 49 ffeeddccbbaa99887766554433221100 00112233445566778899aabbccddeeff
mac_i:       c751f46d585b7963f3c357ce2609d3f419c519d3ff9a81acfd6f668574675a0e
```

(Spaces in the input lines only separate role byte and nonces; they are not part of the input.)

### 6.6 Failures and timeouts

| Event | Detected by | Action |
| ----- | ----------- | ------ |
| TLS handshake fails (mode mismatch, untrusted cert) | both | log, close (no ERROR: no channel) |
| `proto_ver` ≠ 1 | responder | ERROR `BAD_VERSION`, close |
| remote `peer_id` = own (self-connection) | either | ERROR `AUTH_FAILED`, close |
| wrong `mac_r` / `mac_i` | initiator / responder | ERROR `AUTH_FAILED`, close |
| unexpected message type during handshake | either | ERROR `ILLEGAL_STATE`, close |
| cert CN ≠ HELLO name (TLS) | either | ERROR `AUTH_FAILED`, close |
| handshake not complete within **10 s** (`HANDSHAKE_TIMEOUT`) | either | close |

The 10 s handshake limit covers the **whole** HELLO … AUTH_OK exchange, not each message.

---

## 7. State machine

### 7.1 Diagram

```
                 TCP connect / accept (+ TLS if --tls)
                                │
                                ▼
                         ┌─────────────┐  bad MAC, bad version, self,
                         │  HANDSHAKE  │  wrong message, 10 s timeout
                         │ (CONNECTED) │──────────────────────────────┐
                         └──────┬──────┘                              │
              AUTH_OK sent/recv │ (+ cert CN check in TLS mode)       │
                                ▼                                     │
                       ┌─────────────────┐                            │
                       │  AUTHENTICATED  │────────────────────────────┤
                       └────────┬────────┘                            │
          INDEX received        │                                     │
          (or exchange failed)  ▼                                     ▼
                         ┌─────────────┐  BYE, fatal ERROR,     ┌──────────┐
                         │   SYNCED    │──3 missed PONGs, 40 s ─▶│  CLOSED  │
                         └─────────────┘  silence, I/O error    └──────────┘
             NOTIFY, INDEX_REQ/INDEX, CHUNK_REQ/CHUNK_DATA, PING/PONG
```

AUTHENTICATED and SYNCED accept the same messages; SYNCED only records that the first INDEX
exchange has finished. CLOSED is final: a new connection starts again at HANDSHAKE.

### 7.2 Transition table

| State | Event | Action | Next state |
| ----- | ----- | ------ | ---------- |
| HANDSHAKE | expected next handshake message, valid | continue §6.3 | HANDSHAKE |
| HANDSHAKE | AUTH_OK sent (R) / received (I), cert check passed | start reader; send INDEX_REQ | AUTHENTICATED |
| HANDSHAKE | any other message type | ERROR `ILLEGAL_STATE`, close | CLOSED |
| HANDSHAKE | bad MAC, self-connection, cert mismatch | ERROR `AUTH_FAILED`, close | CLOSED |
| HANDSHAKE | `proto_ver` ≠ 1 | ERROR `BAD_VERSION`, close | CLOSED |
| HANDSHAKE | ERROR received | close (no reply) | CLOSED |
| HANDSHAKE | 10 s elapsed | close | CLOSED |
| AUTHENTICATED | INDEX received for our INDEX_REQ (or the exchange failed or took > 10 s) | compare entries (§9.3) | SYNCED |
| AUTHENTICATED, SYNCED | INDEX_REQ | reply INDEX | same |
| AUTHENTICATED, SYNCED | NOTIFY | compare entry (§9.3) | same |
| AUTHENTICATED, SYNCED | CHUNK_REQ | reply CHUNK_DATA or ERROR `CHUNK_NOT_FOUND` | same |
| AUTHENTICATED, SYNCED | CHUNK_DATA (reply) | verify SHA-256, store or re-request | same |
| AUTHENTICATED, SYNCED | PING | reply PONG (same `REQ_ID`, same `t`) | same |
| AUTHENTICATED, SYNCED | PONG (reply) | update RTT, reset missed count | same |
| AUTHENTICATED, SYNCED | reply with no matching outstanding request | ignore (logged) | same |
| AUTHENTICATED, SYNCED | non-fatal ERROR | fail the matching request, or log | same |
| AUTHENTICATED, SYNCED | valid message type with no handler | ERROR `INTERNAL` `"unsupported"` (request's `REQ_ID`); a stray PONG is ignored (decision (i)) | same |
| AUTHENTICATED, SYNCED | handler raised an exception | ERROR `INTERNAL` `"handler failed"` | same |
| AUTHENTICATED, SYNCED | HELLO, HELLO_ACK, AUTH or AUTH_OK | ERROR `ILLEGAL_STATE`, close (decision (i)) | CLOSED |
| any open state | invalid header or body (§4.2) | ERROR with that code (`REQ_ID` 0), close | CLOSED |
| AUTHENTICATED, SYNCED | fatal ERROR received | fail matching request, close (no reply) | CLOSED |
| AUTHENTICATED, SYNCED | BYE received | close (no reply) | CLOSED |
| AUTHENTICATED, SYNCED | local shutdown / duplicate link | send BYE, close | CLOSED |
| AUTHENTICATED, SYNCED | 3rd consecutive PING without PONG (§8.3) | send BYE `"liveness"`, close | CLOSED |
| AUTHENTICATED, SYNCED | no bytes received for 40 s (decision (m)) | close | CLOSED |
| AUTHENTICATED, SYNCED | EOF, reset or other socket error | close | CLOSED |
| CLOSED | — | all outstanding requests fail; dialer may reconnect (§8.4) | — |

---

## 8. Peer management

### 8.1 Dial rule (§6.4 of PROJECT_CONTEXT)

When peer A learns about peer B from an ANNOUNCE and has no connection to B: if
`A.peer_id < B.peer_id` (plain string comparison of the uppercase hex), **A dials** B at the
datagram's source IP and B's `tcp_port`; otherwise A waits to be dialled. This gives exactly one
dialer per pair in the normal case.

Static peers (`--peer HOST:PORT`, for networks where broadcast does not work) are dialled by
everyone regardless of `peer_id`, because the `peer_id` is not known until the handshake.

### 8.2 Duplicate connections (decision (j))

If two authenticated connections to the same `peer_id` exist (both sides dialled, e.g. through
static peers), both ends keep the one **whose initiator has the lower `peer_id`** and close the
other with BYE `"duplicate"`. Both ends apply the same rule, so they keep the same link. A new
link in the same direction as the existing one replaces it.

### 8.3 Liveness (D13)

- Every **10 s** (`PING_INTERVAL`) each side sends PING on every connection.
- At each tick, if the previous PING still has no PONG, that counts as one missed PONG.
  A PONG resets the count to 0.
- When the count reaches **3** (`MAX_MISSED_PONGS`), the connection is closed with BYE
  `"liveness"`. A peer that dies silently is therefore noticed after about 30 s.
- Independently, a connection on which no bytes at all arrive for **40 s**
  (`PING_INTERVAL × (MAX_MISSED_PONGS + 1)`) is closed (decision (m)).

### 8.4 Reconnect

After a connection closes, the side whose turn it is to dial (8.1) tries again every **5 s**
(`ANNOUNCE_INTERVAL`) while the peer is still in the UDP peer table; static peers are retried
for as long as the program runs. UDP LEAVE or expiry never closes a working TCP connection; it
only stops further dial attempts.

### 8.5 peer_id generation (decision (l))

On first run a peer makes `peer_id = uppercase(hex(4 random bytes))` from a cryptographic RNG
and writes it to `<sync_dir>/.meshsync/peer_id`. Later runs reuse it (an invalid file is
replaced). The id therefore survives restarts, which version vectors (§9.3) rely on.

---

## 9. Sync semantics

Status column: **done** = implemented and tested on `main`; **pending** = specified here,
implementation is in an open issue (`sync/` #9–#14, `transfer/` #15–#16).

| Topic | Where | Status |
| ----- | ----- | ------ |
| Entry and path validation on receipt (9.1, 9.2) | `protocol/messages.py` | done |
| INDEX / NOTIFY / CHUNK_REQ wiring (9.6) | `node.py` | done |
| Scanner, index file, chunker | `sync/scanner.py`, `index.py`, `chunker.py` | pending |
| Version-vector compare, conflicts, tombstones (9.3–9.5) | `sync/vclock.py`, `engine.py` | pending |
| Chunk store (9.7) | `sync/store.py` | pending |
| Scheduler, assembly (9.8) | `transfer/scheduler.py`, `assembler.py` | pending |

### 9.1 Index entry

```json
{"path":"notes/report.txt","size":183204,"file_hash":"<sha256 hex>",
 "chunks":["<sha256 hex>", "..."],"version":{"A1F3C9D2":4,"B77C0E11":2},"deleted":false}
```

| Field       | Type    | Constraint |
| ----------- | ------- | ---------- |
| `path`      | string  | see 9.2 |
| `size`      | integer | ≥ 0, file size in bytes |
| `file_hash` | string  | 64 lowercase hex: SHA-256 of the whole file |
| `chunks`    | array   | 64-lowercase-hex SHA-256 of each chunk, in file order |
| `version`   | object  | version vector: `peer_id` (8 uppercase hex) → integer ≥ 0 |
| `deleted`   | boolean | `true` for a tombstone (9.5) |

Exactly these six keys. Consistency between `size`, `chunks` and `file_hash` is checked by the
receiver when it assembles the file (9.8), not when the entry arrives.

### 9.2 Path rules

`path` is POSIX, relative to the sync root. Received entries are rejected (fatal `BAD_HEADER`)
if the path:

- is empty, or starts with `/` (absolute);
- contains `\`, or a control character (U+0000–U+001F, U+007F) (decision (f));
- has an empty, `.` or `..` segment (so no `a//b`, no trailing `/`, no escaping the root);
- starts with the segment `.meshsync` (`META_DIR`), our metadata folder, which is never synced.

Subdirectories are allowed; a rename travels as a delete plus a create (D14).

### 9.3 Version vectors (D2)

Each entry's `version` maps `peer_id → counter`; a missing key means 0. A local edit increments
the editing peer's own counter. Wall-clock times are never compared across hosts.

Comparing a remote entry R with the local entry L for the same path (no local entry = all
zeros):

| Result | Condition | Action |
| ------ | --------- | ------ |
| Remote dominates | R ≥ L in every counter, > in at least one | fetch R's chunks, assemble, store R |
| Local dominates  | L ≥ R in every counter, > in at least one | nothing (the peer will fetch ours) |
| Equal            | all counters equal | skip |
| Concurrent       | each is greater in some counter | conflict (9.4) |

### 9.4 Conflicts

On a concurrent pair, the version written by the peer with the **higher `peer_id`** keeps the
original name. The other version is saved, on all peers, as `name.conflict-<peername>.ext`
(PROJECT_CONTEXT §7.2). The exact rule for filling `<peername>` and for names without an
extension will be fixed by the engine issue (pending). The surviving entry's version is the element-wise
maximum of both vectors, then the resolving peer increments its own counter, and the result is
sent as NOTIFY like any edit. No data is lost.

### 9.5 Tombstones

Deleting a file creates a tombstone: the entry stays in the index with `"deleted": true`, its
own counter incremented, and **no content** (decision (f)): `size` 0, `chunks` `[]`, and
`file_hash` = SHA-256 of empty content (`EMPTY_FILE_HASH` =
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`). Any other content in a
tombstone is invalid (`BAD_HEADER`). Tombstones travel and compare like edits (9.3) and are kept
forever (no garbage collection in scope).

### 9.6 Index exchange and change propagation

1. When a connection becomes AUTHENTICATED, **each** side sends INDEX_REQ and waits up to 10 s
   (`HANDSHAKE_TIMEOUT`) for INDEX.
2. Every received entry is compared (9.3). Fetches and conflict/delete actions run on worker
   threads, never on the connection's reader thread.
3. The connection becomes SYNCED, even if the exchange failed (decision (m)); NOTIFY keeps
   changes flowing and the next reconnect retries the full exchange.
4. Local changes are detected by scanning every **2 s** (`SCAN_INTERVAL`, D13). Each change
   increments the index revision and the file's own counter, and is sent as NOTIFY to every
   connected peer.

### 9.7 Chunks and the chunk store

- Chunk boundaries are content-defined (D10, Gear rolling hash, FastCDC-style): min 16 KiB
  (`CDC_MIN`), average 64 KiB (`CDC_AVG`), max 256 KiB (`CDC_MAX`). Peers do not need to agree on
  the chunking algorithm to interoperate: chunks are only ever named by their SHA-256.
- Store layout: `<root>/.meshsync/chunks/<hash[:2]>/<hash>`. `.meshsync/` is excluded from
  scanning.

### 9.8 Fetch scheduling and assembly

- Missing chunks are requested rarest-first across the peers that have them, ties broken
  round-robin (D12), at most **8** outstanding CHUNK_REQ per peer
  (`MAX_OUTSTANDING_PER_PEER`). If a peer is lost, its outstanding requests go to other peers.
- A CHUNK_DATA whose SHA-256 does not match, or an ERROR `CHUNK_NOT_FOUND`, makes the receiver
  ask another peer for that chunk.
- Assembly: write to `<root>/.meshsync/tmp/`, verify `file_hash`, then atomically
  `os.replace` into place. A partially received file is never visible.

---

## 10. Errors

ERROR body: `{"code": <int>, "msg": <string>}`. On a fatal error the sender sends ERROR, then
closes; the receiver closes without replying.

| Code | Name              | Fatal | When sent | Receiver action |
| ---- | ----------------- | ----- | --------- | --------------- |
| 0x01 | `BAD_HEADER`      | yes | wrong MAGIC; unknown TYPE (decision (a)); invalid body or entry or path (decision (c)) | close |
| 0x02 | `BAD_VERSION`     | yes | header `VER` ≠ 1, or HELLO `proto_ver` ≠ 1 | close |
| 0x03 | `ILLEGAL_STATE`   | yes | wrong message during the handshake; a handshake message after it (decision (i)) | close |
| 0x04 | `AUTH_FAILED`     | yes | wrong MAC; self-connection; TLS cert CN ≠ HELLO name | close |
| 0x05 | `TOO_LARGE`       | yes | `BODY_LEN` > 1 MiB | close |
| 0x10 | `CHUNK_NOT_FOUND` | no  | reply to CHUNK_REQ for a chunk not in the store | request the chunk from another peer |
| 0x11 | `CHUNK_CORRUPT`   | no  | defined for a chunk that fails its SHA-256 check; not sent by the current code | request the chunk from another peer |
| 0x20 | `BUSY`            | no  | defined for a peer refusing more requests; not sent by the current code | retry later or ask another peer |
| 0x7F | `INTERNAL`        | no  | no handler for a valid message type (`"unsupported"`); handler failed | fail the request; keep the connection |

Fatal codes are exactly `FATAL_ERRORS` in `constants.py`. An ERROR with an unknown code is
itself an invalid body (`BAD_HEADER`).

---

## 11. Security considerations

### 11.1 What each mechanism protects against

| Mechanism | Protects against | Does not protect against |
| --------- | ---------------- | ------------------------ |
| HMAC-tagged ANNOUNCE (D11, §3) | outsiders spoofing peers, fake LEAVEs, steering dials to a hostile IP; replay older than 30 s or of an older `ts` | anyone who has `K`; reading the datagrams (they are not secret) |
| HMAC challenge-response (D4, §6.3) | connections from anyone without `K`; replayed handshakes (fresh nonces); reflection (role byte) | eavesdropping or tampering **after** the handshake in plain mode (no per-message MAC) |
| TLS 1.3, mutual certs (D9, §6.2) | eavesdropping, tampering and connection hijacking; peers without a group-CA certificate | a group member using its own valid certificate (the cert-name rule stops it claiming **another** member's name) |
| Path rules (§9.2) | a peer writing outside the sync root or into `.meshsync/` | a member deliberately sending bad content inside the root |
| SHA-256 per chunk and per file | corrupted or substituted chunks | a member who sends a matching entry and matching chunks of malicious content |

### 11.2 Known limits

- **Replay window.** A datagram captured within 30 s can be re-sent only if its `ts` is newer than
  the last one the receiver accepted from that peer, which a live peer's own ANNOUNCEs make
  unlikely. After a receiver restarts, its replay memory is empty, so a datagram up to 30 s old
  can be accepted once.
- **Clock dependency.** Discovery requires host clocks within 30 s of each other; a host with a
  bad clock is invisible (its datagrams are dropped) but can still be reached through `--peer`.
  Sync decisions never depend on clocks (D2).
- **Plain mode exposes data.** Without `--tls`, file names, index contents and chunk bytes cross
  the network in clear text and can be modified in transit after the handshake. Plain mode
  exists for the Wireshark capture (R5) and as a measurement baseline only.
- **Shared secret.** Every holder of `K` is fully trusted; there is no per-peer revocation in
  plain mode.
- **One-message limit.** An index must fit in a single 1 MiB INDEX body; very large folders would
  need paging (out of scope).
- **Untested concurrency in TLS mode.** Our implementation reads and writes one TLS socket from
  two threads; this is not tested under load (follow-up issue).

---

## 12. Improvements over existing protocols

From PROJECT_CONTEXT §5; each row points to where this spec implements it.

| Protocol | Lack | Our improvement | Section |
| -------- | ---- | --------------- | ------- |
| FTP | plaintext credentials; separate control/data connections; whole-file resend; no integrity check | HMAC auth, secret never on wire; TLS mode; one multiplexed TCP connection; chunk delta; SHA-256 per chunk | §6.3, §6.2, §4.3, §9.7, §9.8 |
| HTTP/1.1 | head-of-line blocking; client-server only; verbose text headers | `REQ_ID` allows out-of-order replies; P2P; 12-byte binary header | §4.3, §1, §4.1 |
| rsync | two hosts only; needs SSH; one-shot; no conflict detection | multi-peer mesh; continuous sync; version vectors | §8, §9.6, §9.3 |
| BitTorrent | immutable content; needs tracker/DHT; no deletes | mutable files; LAN discovery, no tracker; tombstones | §9.3, §3, §9.5 |
| NFS / SMB | central server; unusable offline | serverless; offline edits, catch-up on rejoin | §1, §9.6 |
| Fixed-block sync | an insert near the start shifts every block → everything re-sent | content-defined chunking (D10) | §9.7 |
| mDNS / SSDP | unauthenticated, spoofable discovery | HMAC-tagged ANNOUNCE (D11) | §3.2, §3.4 |
| FTP REST / HTTP Range | resume only from the same server, at a byte offset | resume at chunk granularity from **any** peer | §9.8 |
| Timestamp-based sync tools | clock skew → wrong winner, silent data loss | version vectors; conflict copies | §9.3, §9.4 |

---

## 13. Decisions and changelog

### 13.1 Decisions beyond PROJECT_CONTEXT.md

| ID  | Decision | Section |
| --- | -------- | ------- |
| (a) | An unknown TYPE in a TCP header is fatal `BAD_HEADER`. | §4.2 |
| (b) | Discovery replay: drop a datagram whose `ts` ≤ the last accepted `ts` from that `peer_id`; the memory survives LEAVE and expiry. Senders keep `ts` strictly increasing. | §3.4, §3.5 |
| (c) | An invalid TCP body (not UTF-8 JSON, too deeply nested, missing or extra key, wrong type or format, invalid entry or path) is fatal `BAD_HEADER`; §6.8 has no dedicated code. | §4.2 |
| (d) | Hex case is fixed: `peer_id` uppercase, all other hex lowercase; other case is invalid. Needed because `peer_id`s are compared as strings. | §1 |
| (e) | TLS: hostname checking off (dial by IP); after the HMAC handshake, cert CN must equal the HELLO name, else `AUTH_FAILED`. | §6.2 |
| (f) | A tombstone has `size` 0, `chunks` `[]`, `file_hash` = SHA-256 of empty content. Paths must not contain control characters. | §9.5, §9.2 |
| (g) | MAC input = role byte + raw (hex-decoded) nonces, 33 bytes; MAC sent as lowercase hex. Handshake uses `REQ_ID` 0. | §6.4, §6.3 |
| (h) | REQ_IDs: initiator odd, responder even, 0 never used for a request. | §4.3 |
| (i) | After authentication, a handshake message is fatal `ILLEGAL_STATE`; a valid type with no handler gets non-fatal `INTERNAL` `"unsupported"`. | §7.2 |
| (j) | Duplicate links to one peer: keep the one whose initiator has the lower `peer_id`, close the other with BYE `"duplicate"`. | §8.2 |
| (k) | The wiring between net, sync and transfer lives in `src/meshsync/node.py` (not listed in §8.2), which also defines `Action`. | §9.6 |
| (l) | Own `peer_id` = 4 random bytes as uppercase hex, persisted in `<sync_dir>/.meshsync/peer_id`. | §8.5 |
| (m) | *Proposed, awaiting approval in #8.* Liveness and sync details taken from the code: PING is a request (non-zero `REQ_ID`) and PONG echoes it; RTT is measured on the local monotonic clock, `t` is only echoed; a link silent for 40 s is closed; a link becomes SYNCED even if the first INDEX exchange fails. | §5.10, §5.11, §8.3, §9.6 |
| (n) | *Proposed, awaiting approval in #8.* The TLS handshake is bounded by `HANDSHAKE_TIMEOUT` per socket operation, separately from the 10 s HMAC handshake limit. | §6.2 |

### 13.2 Changelog

| Date       | Change |
| ---------- | ------ |
| 2026-11-02 | Initial version from PROJECT_CONTEXT.md §6, decisions (a), (b) |
| 2026-11-07 | TLS mode: wrap before HELLO, hostname check off, cert-CN = HELLO name, decision (e) |
| 2026-11-07 | Decisions (c) invalid body → fatal BAD_HEADER, (d) hex case, (f) tombstone shape and path control chars (#31) |
| 2026-11-07 | Discovery drop-rule order, replay state kept after LEAVE, sender ts strictly increasing (#1) |
| 2026-11-07 | Decision (g): HMAC input encoding; REQ_ID 0 during handshake (#4) |
| 2026-11-07 | REQ_ID odd/even by role (h); cert-CN check in `establish()`; states, ILLEGAL_STATE after auth, unsupported type → INTERNAL (i) (#2) |
| 2026-11-07 | Peer management: dial rule, duplicate-link rule (j), liveness, reconnect (#3) |
| 2026-11-08 | Node wiring: peer_up → INDEX exchange → SYNCED; decisions (k), (l) (#40) |
| 2026-11-08 | Consolidated into one document (#8): overview, field tables, worked examples generated from the code, per-message sections, transition table, error actions, security, improvements map; decisions (m), (n) proposed in #8 for code behaviour that differs from or is missing in PROJECT_CONTEXT.md |
