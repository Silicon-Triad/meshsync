# data/deploy: 3-host deployment session (R3)

Evidence that meshsync runs across three independent hosts on one LAN, discovering peers over
UDP and syncing over TCP, in plain and TLS mode (PROJECT_CONTEXT.md §2 R3, §14 network check).

Owner: Huzaifa Hanif (@Huzaifa-Hanif-01); this session was run by Mujtaba Jawaid Rao.
Related issues: #67 (this session), #74 (report R3 section uses it).
Rules: [`AGENTS.md`](../../AGENTS.md).

Session date: 2026-11-12. Code: `main` at `79854e6`.

## Files

| Path | What |
| ---- | ---- |
| `<host>/host.txt` | hostname, OS, kernel, venv Python, `ip -4 addr`, `ip route`, sockets on the meshsync ports, firewall rules |
| `<host>/plain.log` | `meshsync run --plain --log-level INFO` for the whole plain-mode run |
| `<host>/tls.log` | `meshsync run --tls --log-level INFO` for the TLS run |

Redactions (privacy, nothing technical changed): the Toshiba's hostname and Linux user name were
replaced by `defalt`, and the `altname wlx<MAC>` lines were removed from `ip addr` (they hold the
Wi-Fi MAC address). The first commit of PR #78 still contains those lines; squash merge keeps
them off `main`, but they remain visible in that PR's history.

## Hosts and IP addressing plan

One Wi-Fi LAN, `192.168.100.0/24`, gateway `192.168.100.1`. This is the network we will
demo on (confirmed with Muhammad Ahmed Qazi on 2026-11-13), as #67 requires. Every host is on the same subnet,
so discovery broadcasts and TCP connections stay on the LAN (§14).

| meshsync name | Machine | OS (kernel) | Python | Interface | IPv4 | Address from | Firewall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `kratos` | laptop | Omarchy 4.0.6 (Linux 7.2.8) | 3.14.8 | `wlp2s0` | 192.168.100.92/24 | DHCP | ufw active, default deny in; 47000/udp and 47001/tcp already allowed |
| `minipc` | mini PC | Debian 13 trixie (Linux 6.12.113) | 3.13.5 | `wlp2s0` | 192.168.100.190/24 | static | no ufw; nftables INPUT policy accept (firewall section in `host.txt` trimmed to that) |
| `defalt` | laptop (Toshiba Tecra R950) | Ubuntu 20.04.6 LTS (Linux 5.15.0) | 3.11.18 | `wlp4s0` | 192.168.100.80/24 | DHCP | ufw inactive, no nft |

Ports (from `protocol/constants.py`): UDP 47000 discovery, TCP 47001 data. One peer per host, so
every host used 47001. `host.txt` shows both bound on `0.0.0.0` on all three hosts.

Other interfaces, not used by meshsync: `kratos` and `minipc` run WireGuard (`wg0`, 10.200.200.0/24),
and `minipc` has a libvirt bridge (`virbr1`, 10.10.10.0/24, link down). Peers connected over the
`192.168.100.x` addresses (see the `peer up` lines in the logs).

### For the network diagram (#74)

Three hosts, each one Wi-Fi link to the same access point / router (192.168.100.1). Label each host
with its name, IP and interface from the table. Show UDP 47000 discovery as broadcast on the LAN
and the three TCP 47001 connections as a full mesh: `kratos`→`minipc`, `kratos`→`defalt`,
`minipc`→`defalt` (arrow = side that dialled, from the `initiator handshake` lines).

## What was checked

All three nodes were started **without `--peer`**, so every connection below came from UDP
discovery. Times are from the `minipc`/`defalt` logs; `kratos` logs are 5 h ahead (its clock is
on a different time zone, the seconds agree).

| # | Check (§14) | Evidence | Result |
| - | ----------- | -------- | ------ |
| 1 | Same subnet, `ping` each pair | `host.txt` (same /24); `data/baseline/raw/ping_*` (#70, same demo network: 5 × 10 pings per ordered pair) | all pairs reply, 0% loss |
| 2 | UDP 47000 discovery | `peer up` in `plain.log` with no static peers (`peers=()`) | full mesh found |
| 3 | TCP 47001 through the firewall | `handshake ok` (HMAC, D4) on all 3 links | all 3 links up |
| 4 | Initial sync | existing files on `kratos` assembled on `minipc` and `defalt` (20:27) | ok |
| 5 | Create | `from-minipc.txt` created on `minipc`, fetched by `kratos` and `defalt` (20:29:35, 47 B) | ok |
| 6 | Edit | line appended on `defalt`, fetched by `kratos` and `minipc` (20:30:37, 64 B = 47 + 17) | ok |
| 7 | Delete | `rm` on `kratos`; `minipc` and `defalt` log `deleted ... (remote tombstone)` (20:31:35) | ok |
| 8 | TLS 1.3 + HMAC (D9, D4) | `tls.log`: `(tls)` on all 3 links, `SYNCED` | ok |
| 9 | Sync over TLS | `from-defalt-tls.txt` created on `defalt`; SHA-256 `9588…8724` identical on all 3 hosts | ok |

## Problems and fixes

| Problem | Fix |
| ------- | --- |
| `defalt` had Python 3.8.10; meshsync needs 3.11+ (`pyproject.toml`) | Installed `python3.11` and `python3.11-venv` from the deadsnakes PPA; venv on 3.11.18 |
| Client-to-client traffic on the Wi-Fi (§14 hotspot warning) | Not a problem on this router: all pings and connections worked |
| Firewalls | No change needed: `kratos` ufw already allowed 47000/udp and 47001/tcp; the other two accept all input |
| Code transfer to hosts without the repo | `git archive` of `main` copied with `scp` (no `.git`, `.venv`, certs or keys) |
| Certs and secret distribution | `scripts/gen_certs.sh` on `kratos`; each host got `ca.crt`, its own cert/key and the secret over `scp`. `ca.key` stayed on `kratos`. None are committed. |

## Observations (not failures)

- **`CHUNK_NOT_FOUND`, then fallback.** A receiver asked both connected peers for a new chunk;
  the one that had not received it yet answered `CHUNK_NOT_FOUND` and the scheduler fetched it from
  the other peer. This is the designed fallback (§10) working on a real network. It costs one
  extra round trip per chunk when a file is still spreading.
- **Ping times to `minipc` were uneven** in the step-1 check (45–133 ms from `kratos`, 49–73 ms
  from `defalt`, against 3–9 ms between the two laptops). Likely Wi-Fi power saving on the mini PC.
  It did not affect the run, but matters for the RTT measurements (#70).
- `kratos` runs Python 3.14.8, newer than the CI matrix (3.11, 3.12); it worked without errors.
