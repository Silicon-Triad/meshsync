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

Certificates for TLS mode: `scripts/gen_certs.sh certs ahmed mujtaba huzaifa` (never commit the output).

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
