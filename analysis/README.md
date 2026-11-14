# analysis

Turns the §11 measurement CSVs in `data/` into tables and graphs for the report (R6).

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01). Related issues: #20, #71 (this script).
Rules: [`AGENTS.md`](../AGENTS.md).

```bash
pip install -e .[analysis]          # matplotlib, analysis-only (D15); tables work without it
analysis/analyze.py                 # data/ -> analysis/out/
analysis/analyze.py --no-plots      # tables only
```

- Input: every `data/<experiment>/run<k>.csv` from `scripts/measure/run.py` (#20, #69).
- Output in `analysis/out/` (gitignored until real data exists): per experiment a table
  (`.md` for the report, `.csv`) and a bar chart with ±1 sd error bars (`.pdf` for the report,
  `.png` to preview); `savings.md` with the §13 bytes saved (delta vs full, CDC vs fixed),
  counted as chunk data only and as all bytes received (`recv_bytes`, metadata included).
- Statistics: a run's receivers are averaged first, then mean ± sample sd over runs (n = runs).
  Rows flagged `suspect=dup` (#60) are excluded and the count is printed and noted in the table.
- Throughput = `chunk_bytes / transfer_s` in MB/s (10^6 bytes); undefined when `transfer_s` is 0.
- The delay-model explanation, anomalies and limitations (#71) are written from the real
  3-host numbers, not generated.
