# meshsync

Serverless peer-to-peer file synchronisation (CS-327 CEP, group G2). Three Linux laptops each
hold a shared folder; any create, edit or delete on one host propagates to the others within
seconds, with no central server. Every host runs the same program as an equal peer, speaking our
own application protocol directly on sockets: UDP discovery, TCP for everything else, chunk-level
delta sync verified by SHA-256, multi-source download, version-vector conflict detection, HMAC
authentication and a TLS 1.3 mode.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e .[dev]
meshsync run --dir ~/MeshSync --name ahmed --secret-file ~/.meshsync/key --tls --cert-dir ~/.meshsync/certs
meshsync status
meshsync stats
```

### Local testing

`scripts/run_local_mesh.sh [-n 3] [--tls] [--clean] [--base-dir ./.localmesh]` starts N peers on
this machine (TCP 47001..4700N, shared UDP 47000), each with its own `--dir` and log under
`.localmesh/peerK/`. Ctrl-C stops them all; `--clean` wipes `.localmesh/` first.

### TLS mode

With `--tls`, every TCP connection is wrapped in TLS 1.3 with mutual certificates from the group
CA before HELLO (`src/meshsync/net/tls.py`, `docs/PROTOCOL.md` §6). Generate certificates with
`scripts/gen_certs.sh certs ahmed mujtaba huzaifa` (never commit the output), then copy to each
laptop's `~/.meshsync/certs/`: `ca.crt`, `<name>.crt` and `<name>.key` for that laptop's
`--name`. `ca.key` stays offline with one member. `--plain` skips TLS.

## Config file

Optional TOML file, default `~/.meshsync/config.toml` (`--config PATH` overrides). Keys match
the `run` flags; a CLI flag beats the file, the file beats the built-in default.

```toml
sync_dir = "~/MeshSync"
name = "ahmed"                  # must equal the cert name, [a-z0-9_-]{1,32}
secret_file = "~/.meshsync/key" # chmod 600; only the path goes here
tls = true
cert_dir = "~/.meshsync/certs"  # ca.crt, ahmed.crt, ahmed.key
tcp_port = 47001
udp_port = 47000
chunking = "cdc"                # or "fixed"
peers = ["192.168.1.12:47001"]  # optional static peers
log_level = "INFO"
```

## Tests and lint

```bash
pytest -q
ruff check .
ruff format --check .
```

## Documentation

- [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md): single source of truth (decisions, plan, conventions)
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md): protocol specification
- [`docs/AI_USE_LOG.md`](docs/AI_USE_LOG.md): AI use log
- [`AGENTS.md`](AGENTS.md): rulebook for humans and AI agents (`CLAUDE.md` imports it)
- [`CONTRIBUTING.md`](CONTRIBUTING.md): quick start for contributors
- [`docs/setup/github.md`](docs/setup/github.md): GitHub repository settings

## Team

| Member              | Roll No. | Implementation                                                    |
| ------------------- | -------- | ----------------------------------------------------------------- |
| Muhammad Ahmed Qazi | CS-24045 | Discovery, connection manager, framing, auth, TLS, state machine  |
| Mujtaba Jawaid Rao  | CS-24047 | Scanner, index, chunking, sync engine, version vectors, conflicts |
| Huzaifa Hanif       | CS-24039 | Transfer manager, metrics, measurement scripts                    |
