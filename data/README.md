# data

Measurement results as CSV, `data/<experiment>/<run>.csv`; `data/raw/` is gitignored.

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01). Related issues: #20, #67 (`deploy/`: 3-host deployment evidence), #68 (`captures/`: Wireshark captures), #70 (`baseline/`: ping, iperf3).
Rules: [`AGENTS.md`](../AGENTS.md).

## §11 multi-host results (#77)

`full_delta/`, `fixed_cdc/`, `sources/`, `impaired/`, `tls/`: `run1.csv`–`run5.csv` each, written
by `scripts/measure/hosts.py all ...` (columns: `scripts/measure/run.py` `FIELDS`). Tables and
graphs: `analysis/analyze.py`.

Setup, 2026-11-18/19, one batch on Wi-Fi (same LAN as `deploy/` and `baseline/`):

- Hosts `kratos` (driver, source, netem on its Wi-Fi interface), `minipc`, `defalt`; one peer
  each, plain HMAC except the `tls` variant.
- `defalt`'s Wi-Fi was lossy during the batch (ping from `kratos`: 25–83 % loss, spikes to
  hundreds of ms, at a −39 dBm signal; Wi-Fi power saving was turned off part-way). Full-file
  times therefore vary a lot between runs (e.g. `fixed_cdc` fixed: 55–213 s); byte counts do
  not. Read time differences smaller than the spread (`sources`, `impaired` delay, `tls`) as
  "no clear effect" on this network.
- Interruptions, all redone with `--resume` (only missing `run<k>.csv` are rerun):
  `full_delta` run 4 (SSH connection dropped), `fixed_cdc` run 1 (timeout on the lossy link),
  `fixed_cdc` run 2 (`minipc` powered off by accident), `impaired` run 5 (`defalt` dropped off
  the network under 5 % loss).
- `tls/run4.csv`: `defalt` received 131,673,162 chunk bytes for the 104,857,600-byte file
  (duplicate fetches, #60); the row is flagged `suspect` and `analyze.py` leaves it out.
