# data/baseline: network baseline between the 3 hosts (R6, §11)

RTT (`ping`) and the throughput ceiling (`iperf3`) for every ordered pair of hosts, cross-checked
with meshsync's own liveness PING/PONG. Issue #70.

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01); this session was run by Mujtaba Jawaid Rao.
Related issues: #70, #71 (analysis), #67 (hosts, `data/deploy/`).
Rules: [`AGENTS.md`](../../AGENTS.md).

Session date: 2026-11-14, on the demo network (`192.168.100.0/24`, see `data/deploy/README.md`).
Hosts: `kratos` (laptop, .92), `minipc` (mini PC, .190), `defalt` (laptop, .80); all on Wi-Fi.

## Files

| Path | What |
| ---- | ---- |
| `raw/ping_<src>_<dst>_run<k>.txt` | `ping -c 10` output, 5 runs per ordered pair |
| `raw/iperf3_<src>_<dst>_run<k>.json` | `iperf3 -c <dst> -t 10 -J` output (server on dst), 5 runs per ordered pair |
| `rtt.csv`, `iperf3.csv` | one row per run (from `scripts/measure/baseline.py`) |
| `summary.csv` | mean ± sample sd over the 5 runs, per ordered pair |
| `pingpong/<host>-metrics.csv` | `scripts/measure/peer.py` metrics CSV, plain mode, about 3 min |
| `pingpong/<host>-pingpong.log` | that run's `--log-level INFO` log |
| `pingpong.csv` | meshsync PING → PONG round trips, one row per pair |

Reproduce: `scripts/measure/baseline.sh data/baseline/raw kratos:192.168.100.92
minipc:192.168.100.190:<user>@192.168.100.190 defalt:192.168.100.80:<user>@192.168.100.80`, then
`scripts/measure/baseline.py data/baseline/raw --out data/baseline --metrics kratos=... ...`.
Method settings (not protocol values): 5 runs (§11), 10 pings per run, 10 s per iperf3 run
(iperf3's default). Redacted: the `defalt` hostname and Linux user name (privacy).

## Results (mean ± sd, n = 5 runs; PING/PONG n = 20–21)

| Direction | `ping` RTT (ms) | meshsync PING/PONG (ms) | iperf3 (Mbit/s) |
| --------- | --------------- | ----------------------- | --------------- |
| kratos → defalt | 9.70 ± 10.27 | 10.91 ± 5.59 | 16.58 ± 0.78 |
| defalt → kratos | 8.55 ± 3.42 | 10.87 ± 4.82 | 7.66 ± 1.26 |
| kratos → minipc | 68.02 ± 25.97 | 71.82 ± 29.38 | 14.29 ± 3.62 |
| minipc → kratos | 14.24 ± 10.78 | 13.19 ± 3.56 | 27.59 ± 1.41 |
| minipc → defalt | 17.34 ± 13.81 | 17.15 ± 14.62 | 10.45 ± 2.63 |
| defalt → minipc | 56.53 ± 5.61 | 35.98 ± 20.42 | 10.38 ± 1.34 |

iperf3 is TCP, receiver side, 1 Mbit = 10^6 bit. "src → dst" is the direction data flows
(iperf3) or the side that sends the echo request (ping, PING).

## What the numbers say

- **Throughput ceiling 7.7–27.6 Mbit/s.** All three hosts are Wi-Fi clients of one access point,
  so every frame crosses the air twice (sender → AP → receiver), sharing one channel. This is the
  upper bound for any meshsync transfer between these hosts: a 100 MiB file needs at least
  roughly 30–110 s of transfer here (100 MiB × 8 / ceiling).
- **Direction matters.** `defalt` sends at 7.7 Mbit/s but receives at 16.6 (older Wi-Fi card);
  `minipc` sends at 27.6 but receives at 10–14.
- **meshsync PING/PONG agrees with `ping`** within about 1–4 ms on five of six directions. The
  small excess is meshsync's own processing (Python, framing, the reader thread), i.e. d_proc in
  d_nodal = d_proc + d_queue + d_trans + d_prop (§11). It validates the protocol's RTT samples.

## Anomalies

- **Reaching `minipc` is slow (57–68 ms) but `minipc` reaching others is fast (14–17 ms).**
  Wi-Fi power saving is **on** on `minipc` (`iw dev wlp2s0 get power_save` → `Power save: on`)
  and **off** on `defalt`: the access point buffers frames for a sleeping client until it wakes,
  so traffic *to* `minipc` waits, while traffic it initiates finds its radio awake.
- **`defalt → minipc`: PING/PONG 36 ms vs `ping` 57 ms**, the one direction that disagrees. Both
  wait for `minipc`'s radio to wake, so the result depends on when each probe lands relative to
  its sleep cycle (high sd, 20 ms); not a measurement error.
- Power saving was left as found, so the numbers describe the demo setup as it is. Whether to
  turn it off for the §11 runs (#77) is a team decision.

## Problems and fixes

| Problem | Fix |
| ------- | --- |
| `kratos → defalt` ping failed once with `No route to host`; `defalt`'s SSH session dropped and reconnected several times | `defalt` briefly left the Wi-Fi (its power saving is off; likely the old card or near-suspend). Kept its lid open and on power; `baseline.sh` was made resumable and re-ran only the failed runs |
| iperf3 runs to/from `defalt` failed: `iperf3: command not found` | iperf3 had not been installed there; installed (3.7), re-ran |
| `minipc → kratos` iperf3 run 5: connection refused | the one-off server on `kratos` was not up yet; succeeded on the re-run |
| `kratos`'s ufw blocks iperf3 (TCP 5201, iperf3's default port) | `ufw allow 5201/tcp` for the session, removed afterwards |

## Connection reuse vs reconnect (§12)

No separate experiment: the cost follows from the handshake in `PROTOCOL.md` §5 and the RTTs
above. Before an operation can run on a **new** connection, these round trips are needed:
TCP connect (1), HELLO / HELLO_ACK (1), AUTH / AUTH_OK (1) and INDEX_REQ / INDEX to reach SYNCED
(1); TLS 1.3 adds 1 more. A **persistent** connection pays only the operation's own round trip.

| Pair (`ping` RTT) | persistent: 1 RTT | reconnect, plain: 5 RTT | reconnect, TLS: 6 RTT |
| ----------------- | ----------------- | ----------------------- | --------------------- |
| kratos ↔ defalt (≈ 9 ms) | ≈ 9 ms | ≈ 46 ms | ≈ 55 ms |
| kratos → minipc (≈ 68 ms) | ≈ 68 ms | ≈ 340 ms | ≈ 408 ms |

These are model estimates from measured RTTs, **not** measurements, and they leave out handshake
processing (the #79 single-host pilot measured about 8 ms plain and 52 ms TLS setup with near-zero
RTT, so the real reconnect cost is higher still). Reconnecting per operation would make every
small operation 5–6× slower; a persistent full mesh avoids that at the price of O(n²)
connections, which is cheap at 3 peers. Proposal on #70: use this derivation for the tension
instead of a new experiment.
