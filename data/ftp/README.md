# data/ftp: FTP baseline on the 3 hosts

Plain FTP measured on the same hosts, LAN, test file and edits as the §11 meshsync runs, for the
comparison with an existing protocol (`PROJECT_CONTEXT.md` §5). Written by
`scripts/measure/ftp_baseline.py`. Issue #95.

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01). Related issues: #95, #77 (the meshsync runs compared here).
Rules: [`AGENTS.md`](../../AGENTS.md).

## Setup (2026-11-21, Wi-Fi)

- **Server:** `kratos` (192.168.100.92), vsftpd with a throwaway user, anonymous login off,
  passive data ports 40000–40100. Both opened in ufw for 192.168.100.0/24 only.
- **Clients:** `minipc` (.190) and `defalt` (.80), Python `ftplib` (standard library), both
  downloading at the same time, as the two meshsync receivers did.
- **Files:** the #25 test data: set A (`large/large.bin`, 100 MiB) and set D (`edits/`).
  Every download was checked against the SHA-256 in the driver's manifest; all passed.
- **Variants per run:** `full` (A), `overwrite` (A with 1 KiB overwritten), `insert` (A with
  1 KiB inserted at offset 0), `loss` (A with 5 % netem loss on kratos's Wi-Fi).
- `runs.log` gives each run's start and end. Run 3 failed the first time (below) and was redone
  with `--resume`.

## Results (mean ± sd, n = 5; the two clients are averaged within a run, as in `analyze.py`)

| Variant | Transfer time (s) | Bytes downloaded | meshsync, same edit (`data/`) |
| ------- | ----------------- | ---------------- | ----------------------------- |
| full | 106.5 ± 32.5 | 104,857,600 | `impaired` clean 116.4 ± 33.6 s, 104,874,436 chunk bytes |
| overwrite (1 KiB) | 88.5 ± 17.3 | **104,857,600** (whole file) | `full_delta` delta: **75,667** chunk bytes, 5.2 ± 10.9 s |
| insert (1 KiB at 0) | 87.8 ± 33.4 | **104,858,624** (whole file) | `fixed_cdc` cdc: **66,594** chunk bytes, 0.30 ± 0.41 s |
| loss (5 %) | 248.9 ± 56.2 | 104,857,600 | `impaired` loss 474.2 ± 143.7 s (another night, see below) |

- **After a small edit FTP downloads the whole file again**: about 1,386× (overwrite) and
  1,575× (insert) the bytes meshsync sent. FTP has no delta transfer.
- **A fresh full copy is about as fast with FTP as with meshsync** (one plain TCP stream;
  the Wi-Fi is the bottleneck for both). The meshsync advantage is after edits, in
  authentication and in integrity, not in raw bulk speed.
- **Loss:** FTP took 2.3× its full-copy time under 5 % loss; meshsync took 4.1× in its own
  batch (`data/impaired`). Those meshsync runs were on 2026-11-18/19, when `defalt`'s Wi-Fi was
  much worse (`data/README.md`), so this cross-night ratio is only indicative.
- `defalt` was 2–3× slower than `minipc` in every variant: its Wi-Fi, as in the meshsync runs.

## What the capture shows (FTP weaknesses from §5)

One `full` download by `minipc`, captured on kratos with
`tshark -s 200 -f "host 192.168.100.190 and (tcp port 21 or tcp portrange 40000-40100)"`.
The capture is **not committed**: it contains the throwaway password in plain text.

| Frame | From | FTP | Meaning |
| ----- | ---- | --- | ------- |
| 4 | server | `220` | ready |
| 6 | client | `USER ftptest` | user name in plain text |
| 9 | client | `PASS ********` | **password in plain text** (masked here, readable in the capture) |
| 11 | server | `230` | logged in |
| 12 | client | `TYPE I` | binary mode |
| 15 / 16 | client / server | `PASV` / `227` | server opens a **separate data port**, 40029 |
| 20 | client | `RETR large.bin` | request the file |
| 21 | server | `150` | data connection opening |
| 77938 | server | `226` | transfer complete |
| 77939 / 77940 | client / server | `QUIT` / `221` | goodbye |

TCP conversations: **two** connections, `:21` (28 frames, 2,217 bytes: commands) and `:40029`
(77,918 frames, 110 MB: the file). Against meshsync:

| §5 claim about FTP | Evidence here | meshsync |
| ------------------ | ------------- | -------- |
| plaintext credentials | `PASS` readable (frame 9) | HMAC handshake: only nonces and MACs on the wire (`data/captures/`, cap2b) |
| separate control and data connections | `:21` + `:40029` per transfer | one TCP connection per peer pair (47001) |
| whole-file resend | overwrite / insert rows above | chunk delta (`full_delta`, `fixed_cdc`) |
| no integrity check | nothing in FTP checks the file; the client script had to compare SHA-256 | SHA-256 per chunk and per file |

## Problems and fixes

| Problem | Fix |
| ------- | --- |
| `minipc` and `defalt` hold a copy of the repo, not a git clone | copied `ftp_baseline.py` to them with `scp` |
| kratos runs Omarchy (Arch): no apt; vsftpd allows anonymous login and not local users by default | `pacman -S vsftpd`; set `anonymous_enable=NO`, `local_enable=YES`, passive ports 40000–40100 |
| ufw blocked FTP | allowed TCP 21 and 40000–40100 from 192.168.100.0/24 only; to be removed after the session |
| `tshark -w ~/…` as root: permission denied | wrote to `/tmp`, then moved the file |
| run 3: the 5 % loss also dropped the shared SSH connections; without keys, the two clients' password prompts collided and runs 3–4 failed | SSH keys for the session (to be removed afterwards); `--resume` redid runs 3–5 |
