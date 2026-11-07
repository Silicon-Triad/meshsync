<!-- FROZEN INTERFACE (AGENTS.md §6): any change or addition needs an issue and approval from all three members first (R-6.2). -->

# meshsync Protocol Specification (version 1)

This document specifies the meshsync wire protocol completely enough for an independent
implementation. It is derived from `docs/PROJECT_CONTEXT.md` §6; any protocol change must update
this file in the same PR.

All peers are equal (pure peer-to-peer, full mesh). `K` denotes the group's shared secret.
All multi-byte integers are in network byte order (big-endian).

## 1. Transport

| Traffic         | Transport                 | Port  |
| --------------- | ------------------------- | ----- |
| ANNOUNCE, LEAVE | UDP broadcast             | 47000 |
| All else        | TCP (optionally TLS 1.3)  | 47001 |

Justification:

- **Discovery over UDP.** Peers must be found without knowing each other's addresses, which
  requires broadcast; TCP cannot broadcast. Discovery is soft state: a lost ANNOUNCE is replaced
  by the next one 5 s later, so UDP's lack of reliability is harmless and costs nothing.
- **Everything else over TCP.** Index entries and file chunks must arrive complete, uncorrupted
  and in order; TCP provides reliability, ordering and congestion control. Timing is not
  critical (seconds-level sync), so TCP's retransmission latency is acceptable. TLS runs over
  TCP, giving the encryption mode for free.
- One persistent TCP connection per peer pair carries all messages, multiplexed by `REQ_ID`.

## 2. Discovery datagram (UDP)

A single UTF-8 JSON object, at most 512 bytes:

```json
{"magic":"MS","ver":1,"type":"ANNOUNCE","peer_id":"A1F3C9D2","name":"ahmed",
 "tcp_port":47001,"index_rev":57,"ts":1730473200,"tag":"<hex HMAC>"}
```

| Field       | Type   | Meaning                                         |
| ----------- | ------ | ----------------------------------------------- |
| `magic`     | string | Always `"MS"`                                   |
| `ver`       | int    | Protocol version, `1`                           |
| `type`      | string | `"ANNOUNCE"` or `"LEAVE"`                       |
| `peer_id`   | string | 8 hex characters, unique per peer               |
| `name`      | string | Human-readable peer name                        |
| `tcp_port`  | int    | Port the peer accepts TCP connections on        |
| `index_rev` | int    | Sender's current index revision                 |
| `ts`        | int    | Sender's Unix time in seconds                   |
| `tag`       | string | Hex `HMAC-SHA256(K, canonical_json_without_tag)` |

Rules:

- Senders broadcast ANNOUNCE every **5 s**. On shutdown they send LEAVE (same shape,
  `"type":"LEAVE"`).
- Receivers drop the datagram if the tag is wrong or `|now - ts| > 30 s`.
- **Replay within the window (decision b):** receivers should also drop a datagram whose `ts` is
  less than or equal to the last accepted `ts` from that `peer_id`. (Implemented in
  `net/discovery.py`.)
- A peer not heard from for **15 s** is removed from the peer table. LEAVE removes it at once.

## 3. TCP frame

Every TCP message is a 12-byte header followed by a body (`struct` format `!2sBBII`):

| Offset | Size | Field    | Notes                                                    |
| ------ | ---- | -------- | -------------------------------------------------------- |
| 0      | 2    | MAGIC    | `b"MS"`                                                  |
| 2      | 1    | VER      | `1`                                                      |
| 3      | 1    | TYPE     | Message type (§4)                                        |
| 4      | 4    | REQ_ID   | uint32; a reply echoes the request's ID; 0 = unsolicited |
| 8      | 4    | BODY_LEN | uint32; maximum 1 MiB (1 048 576)                        |
| 12     | N    | BODY     | UTF-8 JSON, or raw bytes for CHUNK_DATA                  |

Receiver validation, in order: bad MAGIC → `BAD_HEADER`; VER ≠ 1 → `BAD_VERSION`;
unknown TYPE → `BAD_HEADER` (**decision a**); BODY_LEN > 1 MiB → `TOO_LARGE`. All are fatal.

Because responses carry the request's `REQ_ID`, they may arrive out of order on one connection
(no head-of-line blocking).

## 4. Message types

I = initiator, R = responder. Dial rule: the peer with the lower `peer_id` initiates.

| Code | Name       | Dir | Body                               | Action on receipt                             |
| ---- | ---------- | --- | ---------------------------------- | --------------------------------------------- |
| 0x01 | HELLO      | I→R | `{peer_id,name,proto_ver,nonce_i}` | Check version → HELLO_ACK                     |
| 0x02 | HELLO_ACK  | R→I | `{peer_id,name,nonce_r,mac_r}`     | Verify `mac_r` → AUTH                         |
| 0x03 | AUTH       | I→R | `{mac_i}`                          | Verify → AUTH_OK, else ERROR 0x04 + close     |
| 0x04 | AUTH_OK    | R→I | `{}`                               | → AUTHENTICATED                               |
| 0x10 | INDEX_REQ  | ↔   | `{}`                               | Reply INDEX                                   |
| 0x11 | INDEX      | ↔   | `{index_rev,files:[entry]}`        | Compare every entry                           |
| 0x12 | NOTIFY     | ↔   | `{entry}`                          | Compare one entry                             |
| 0x20 | CHUNK_REQ  | ↔   | `{hash}`                           | Reply CHUNK_DATA or ERROR 0x10                |
| 0x21 | CHUNK_DATA | ↔   | raw bytes                          | Verify SHA-256; store or re-request elsewhere |
| 0x30 | PING       | ↔   | `{t}`                              | Reply PONG                                    |
| 0x31 | PONG       | ↔   | `{t}`                              | RTT = now − t                                 |
| 0x7E | ERROR      | ↔   | `{code,msg}`                       | Log; close if fatal                           |
| 0x7F | BYE        | ↔   | `{reason}`                         | Close cleanly                                 |

Index entry (used in INDEX and NOTIFY):

```json
{"path":"notes/report.txt","size":183204,"file_hash":"<sha256>",
 "chunks":["<sha256>", "..."],"version":{"A1F3C9D2":4,"B77C0E11":2},"deleted":false}
```

`path` is POSIX, relative to the sync root, and must never contain `..` (reject on receipt).

## 5. Handshake (authentication)

```
I → R  HELLO      nonce_i (16 random bytes, hex)
R → I  HELLO_ACK  nonce_r, mac_r = HMAC-SHA256(K, "R" | nonce_i | nonce_r)
I → R  AUTH       mac_i = HMAC-SHA256(K, "I" | nonce_r | nonce_i)
R → I  AUTH_OK
```

- `|` is concatenation. The secret `K` never crosses the wire.
- MACs are compared in constant time (`hmac.compare_digest`).
- A wrong MAC → ERROR `AUTH_FAILED` (0x04) and close.
- The whole handshake must complete within **10 s**, else close.

## 6. TLS mode

- Enabled with `--tls`; `--plain` disables it. Both peers must use the same mode.
- Both sides: TLS 1.3 minimum, `CERT_REQUIRED` (mutual authentication). Certificates are
  issued by a group CA (`scripts/gen_certs.sh`) and are never committed.
- Peers dial by IP, so hostname checking is off; trust comes from the CA chain. Certificates
  carry `SAN=DNS:<peer name>`.
- The socket is wrapped immediately after `connect()`/`accept()`, before HELLO. The protocol
  inside TLS is unchanged (HMAC handshake still runs).
- Mode mismatch or untrusted certificate → TLS handshake fails → logged and closed.

## 7. State machine

```
CONNECTED --HELLO/ACK/AUTH/AUTH_OK--> AUTHENTICATED --INDEX exchange--> SYNCED
   |                                        |                              |  (NOTIFY, CHUNK_*, PING, INDEX_REQ)
   +-- fatal ERROR / 10 s timeout ----------+---- BYE / 3 missed PONG -----+--> CLOSED
```

- Any message other than the next handshake step before AUTHENTICATED → ERROR
  `ILLEGAL_STATE` (0x03) and close.
- PING every 10 s; 3 consecutive missed PONGs → CLOSED.
- At most 8 outstanding CHUNK_REQ per peer.

## 8. Error codes

Sent in an ERROR body as `{"code": <int>, "msg": <string>}`. On a fatal error the sender closes
the connection after sending ERROR.

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

## 9. Decisions beyond PROJECT_CONTEXT.md

- **(a)** An unknown TYPE code in a TCP header is treated as fatal `BAD_HEADER` (0x01).
- **(b)** Discovery replay within the 30 s window: receivers should also reject `ts` ≤ the last
  seen `ts` for that `peer_id`.
- **(f)** A malformed or invalid TCP message body (not UTF-8 JSON, missing/unknown key, wrong
  type or format, invalid index entry or path) is treated as fatal `BAD_HEADER` (0x01), since
  §6.8 has no dedicated code. (Implemented in `protocol/messages.py`.)

## Changelog

| Date       | Change                                                        |
| ---------- | ------------------------------------------------------------- |
| 2026-11-02 | Initial version from PROJECT_CONTEXT.md §6, decisions (a), (b) |
| 2026-11-07 | Decision (f): invalid message body → fatal BAD_HEADER (#31)  |
