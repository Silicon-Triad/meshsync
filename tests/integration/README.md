# tests/integration

Multi-peer tests on localhost: real `meshsync run` processes on `127.0.0.1`, each with its own
`--dir` and `--tcp-port`, like `scripts/run_local_mesh.sh` (§14). Real sockets are allowed here;
AGENTS.md R-4.8's "no real network" rule covers `tests/unit/` only.

- `mesh.py`: `Mesh` / `Peer` helpers (start, stop, wait with a timeout derived from
  `protocol/constants.py`, received-bytes from the DEBUG metrics lines).
- `conftest.py`: `make_mesh` fixture; stops every peer after the test.
- `test_sync.py`: create (static peers, UDP discovery, TLS), small edit (delta), delete,
  offline and rejoin, concurrent edit (conflict copy).
- `test_failures.py`: §10 failure scenarios (#63); table below.

Marked `integration` and `slow`; plain `pytest -q` (and CI) runs them (about 15-25 s).
Run only these with `pytest -q -m integration`, skip them with `pytest -q -m "not integration"`.
On a timeout the assertion message holds the tail of every peer's log.

Owner: all members. Related issues: #62, #63 (also #7, #40, #53, #55).
Rules: [`AGENTS.md`](../../AGENTS.md).

## §10 failure scenarios (R7, #63)

Evidence lines are what the report quotes (`LEVEL logger: message`).

| §10 scenario | Test | Evidence (log line) |
| --- | --- | --- |
| Peer killed mid-transfer | `test_peer_killed_mid_transfer_completes_from_other_peer` | carl: `INFO meshsync.net.manager: peer down: alice ...`, then `INFO meshsync.transfer.scheduler: CHUNK_REQ to <id> failed: ...` or `WARNING meshsync.transfer.scheduler: peer <id> lost during fetch (...); reassigning its chunks`, then `INFO ...scheduler: fetched all chunks of large.bin`; file SHA-256 equals the source. alice is killed on carl's first `recv peer=alice type=CHUNK_DATA`; the test skips if carl already finished. |
| Concurrent edits on 2 hosts | `test_sync.py` (#62) | — |
| Peer offline, files change, rejoin | `test_sync.py` (#62) | — |
| Wrong secret | `test_wrong_secret_error_auth_failed_and_closed` | dialler: `WARNING meshsync.net.auth: initiator handshake failed: AUTH_FAILED: bad mac_r`; listener: `WARNING meshsync.net.auth: responder handshake failed: AUTH_FAILED: peer sent ERROR: ...` and `WARNING meshsync.net.manager: handshake from 127.0.0.1 failed: ...` |
| Spoofed / replayed ANNOUNCE | `test_spoofed_and_replayed_announce_dropped` | `WARNING meshsync.net.discovery: drop <id> from <ip>: bad tag` / `stale ts <ts>` / `replay ts <ts>` |
| Untrusted cert | `test_untrusted_cert_tls_alert_and_closed` | `WARNING meshsync.net.tls: TLS handshake failed (server side): ... CERTIFICATE_VERIFY_FAILED ...` |
| Mode mismatch (`--tls` vs `--plain`) | `test_tls_plain_mode_mismatch_closed` | `WARNING meshsync.net.tls: TLS handshake failed (server side)` and `(client side)` on the `--tls` peer |
| Corrupt chunk (test flag) | not yet: approach under discussion on #63 | — |
| 5 % loss via netem | not automated: needs root; run `scripts/netem.sh` by hand, results in `data/` | — |
