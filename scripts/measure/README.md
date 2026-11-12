# scripts/measure

Experiment runners for the measurement plan (§11, R6).

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01). Related issues: #20.
Rules: [`AGENTS.md`](../../AGENTS.md).

```bash
scripts/gen_testdata.py testdata --sets A,D          # once (#19, data set #25)
scripts/measure/run.py full_delta                     # §11 condition 1, 5 runs
scripts/measure/run.py fixed_cdc                      # condition 2 (backs D10)
scripts/measure/run.py sources                        # condition 3
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
