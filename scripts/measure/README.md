# scripts/measure

Experiment runners for the measurement plan (§11, R6).

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01). Related issues: #20, #68 (anon_mac), #69 (conditions 5-6), #70 (baseline), #77 (hosts).
Rules: [`AGENTS.md`](../../AGENTS.md).

```bash
scripts/gen_testdata.py testdata --sets A,D          # once (#19, data set #25)
scripts/measure/run.py full_delta                     # §11 condition 1, 5 runs
scripts/measure/run.py fixed_cdc                      # condition 2 (backs D10)
scripts/measure/run.py sources                        # condition 3
sudo -E .venv/bin/python scripts/measure/run.py impaired   # condition 5 (netem needs root)
scripts/measure/run.py tls                            # condition 6: plain vs TLS
scripts/measure/run.py all --runs 5
```

- **Single host only** for now: every peer runs on this machine, sharing its CPUs and disk, so
  these are pilot numbers; the 3-laptop runner is #77.
- `run.py` starts a fresh local mesh per run (plain mode, peers on this machine) and writes one
  row per receiving peer to `data/<experiment>/run<k>.csv`. Per-peer metrics CSVs and logs go to
  `data/raw/` (gitignored); the 100 MiB copies are deleted unless `--keep-data`.
- `peer.py` runs one `meshsync run` with the per-message metrics CSV (#17) switched on.
- Columns: `e2e_s` (file placed on the source → complete on the receiver), `notify_s` (the
  source's scan + chunking part of it), `transfer_s` (first CHUNK_REQ → last CHUNK_DATA),
  `chunk_bytes` / `recv_bytes` / `sent_bytes` (application-layer bytes, header + body), and
  `sources` / `source_bytes` (which peers sent chunks; with 3 peers receivers also fetch from
  each other), `e2e_from` (`placed`, or `joiner_start` for condition 3: never compare across
  them), `poll_s` (time resolution) and `suspect` (`dup` = chunks downloaded twice, #60; such
  rows must not be used). A failed run is logged and the batch continues, exiting 1 at the end;
  old `run<k>.csv` files are removed when a batch starts.
- **Condition 5 (`impaired`, #69):** set A over a clean link, then with `scripts/netem.sh`
  loss (`--loss-pct`, default 5 from §10), then delay (`--delay-ms`, default 50, decided on
  #69), on `--iface` (default `lo`). netem is applied after the mesh connects and always cleared
  afterwards, even when a run fails. Needs root or passwordless sudo, and a kernel with the
  netem module. On `lo` both directions cross netem: a delay of D ms adds 2D ms per round trip,
  and loss hits data and returning ACKs alike, so a request/reply exchange fails with
  1 - (1 - p)^2 (about 9.75 % at p = 5 %), harsher than one 5 % Wi-Fi link (#79 review).
- **Condition 6 (`tls`, #69):** set A in plain vs TLS mode (fresh certs per mesh from
  `gen_certs.sh`, under `data/raw/`). Adds the `setup_s` column (empty for other experiments):
  a receiver's "peer manager started" → "connection to <source> is SYNCED" from its own log
  (ms resolution), i.e. TCP connect, TLS handshake, HMAC handshake and INDEX exchange.
  Throughput is `chunk_bytes / transfer_s`, worked out in the analysis (#71).
- Condition 4 (chunk size) is blocked on a frozen-interface decision; condition 7 (Wi-Fi vs
  wired) needs the 3-host runner (#77).
- **Network baseline (#70):** `baseline.sh OUTDIR NAME:IP[:SSH_TARGET] ...` runs `ping` and
  `iperf3` between every ordered pair of hosts (5 runs, §11) from one host over SSH; resumable,
  a failed run is logged and re-run next time. `baseline.py RAW_DIR --out data/baseline
  [--metrics NAME=CSV ...]` writes per-run CSVs, mean ± sd per pair, and the meshsync PING/PONG
  cross-check from `peer.py` metrics CSVs. Results: `data/baseline/README.md`.
- **`anon_mac.py IN.pcapng OUT.pcapng` (#68):** replaces every unicast MAC in a pcapng capture
  with a stable fake (`02:00:00:00:00:NN`) before it is committed; everything else is unchanged.
- **Across separate hosts (#77): `hosts.py EXPERIMENT HOST HOST [HOST ...]`.** Same experiments
  and the same `data/<experiment>/run<k>.csv` columns as `run.py`, but one peer per host. Run it
  on one host; the others are reached over SSH (`NAME:IP:user@IP`, one reused connection each).
  The first host is the source (for `sources` the last is the joiner; for `impaired` the source
  must be the local host, netem goes on its `--iface`, default `wlp2s0`, via cached `sudo`).
  **One clock per number:** `e2e_s` runs from the receiver's first NOTIFY received to its last
  CHUNK_DATA (`e2e_from = first_notify`; `joiner_start` for `sources`), `notify_s` is the
  source's scan time on the source's clock. Each host needs `~/meshsync` with its `.venv`, the
  secret `~/.meshsync/key`, certs named after the host (TLS), and the seeded #25 test data
  (`scripts/gen_testdata.py testdata --sets A,D`; checked by SHA-256 before a run). Turn the
  mini PC's Wi-Fi power saving off for these runs (#83). Raw per-host CSVs and logs:
  `data/raw/hosts/` (gitignored).
- **Unattended batches (#77):** set up SSH keys first (`ssh-copy-id user@host` for each remote
  host); the driver warns at start-up if a host still asks for a password, because a dropped
  connection would then wait for one. SSH heartbeats (15 s) detect a dead link, and read-only
  polls, stopping peers and copying results retry an SSH failure 3 times. A run that still
  fails is skipped; afterwards `hosts.py EXPERIMENT ... --resume` runs only the missing
  `run<k>.csv` files and never clears the existing ones. `data/<experiment>/runs.log` records
  when each run was measured, so runs redone on another day are visible.
