# tests/integration

Multi-peer tests on localhost. Each peer is a real `meshsync run` process started by
[`mesh.py`](mesh.py) with its own folder, secret, free ports and log file; peers connect with
`--peer` (§14). Run: `pytest -q tests/integration` (about 40 s; the first test generates set A).

Owner: all members. Related issues: #63 (§10 failure scenarios), #62 (end-to-end sync).
Rules: [`AGENTS.md`](../../AGENTS.md).

## §10 failure scenarios (R7, issue #63)

Evidence lines are what the report quotes; format is `LEVEL logger: message`.

| §10 scenario | Test | Evidence (log line) |
| --- | --- | --- |
| Peer killed mid-transfer | `test_peer_killed_mid_transfer_completes_from_other_peer` | carl: `INFO meshsync.net.manager: peer down: alice ...`, then `INFO meshsync.transfer.scheduler: CHUNK_REQ to <id> failed: ...` or `WARNING meshsync.transfer.scheduler: peer <id> lost during fetch (...); reassigning its chunks`, then `INFO ...scheduler: fetched all chunks of large.bin`; file hash equals the source |
| Concurrent edits on 2 hosts | covered by #62 | — |
| Peer offline, files change, rejoin | covered by #62 | — |
| Wrong secret | `test_wrong_secret_error_auth_failed_and_closed` | dialler: `WARNING meshsync.net.auth: initiator handshake failed: AUTH_FAILED: bad mac_r`; listener: `WARNING meshsync.net.auth: responder handshake failed: AUTH_FAILED: peer sent ERROR: ...` and `WARNING meshsync.net.manager: handshake from 127.0.0.1 failed: ...` |
| Spoofed / replayed ANNOUNCE | `test_spoofed_and_replayed_announce_dropped` | `WARNING meshsync.net.discovery: drop <id> from <ip>: bad tag` / `stale ts <ts>` / `replay ts <ts>` |
| Untrusted cert | `test_untrusted_cert_tls_alert_and_closed` | `WARNING meshsync.net.tls: TLS handshake failed (client side): ... CERTIFICATE_VERIFY_FAILED ...` and `(server side)` |
| Mode mismatch (`--tls` vs `--plain`) | `test_tls_plain_mode_mismatch_closed` | `WARNING meshsync.net.tls: TLS handshake failed (server side): ...` on the `--tls` peer |
| Corrupt chunk (test flag) | not yet: no test flag exists; blocked on the decision in #63 | — |
| 5 % loss via netem | not automated: needs root; run `scripts/netem.sh` by hand, results in `data/` | — |
