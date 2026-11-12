# tests/integration

Multi-peer tests on localhost: real `meshsync run` processes on `127.0.0.1`, each with its own
`--dir` and `--tcp-port`, like `scripts/run_local_mesh.sh` (§14). Real sockets are allowed here;
AGENTS.md R-4.8's "no real network" rule covers `tests/unit/` only.

- `mesh.py`: `Mesh` / `Peer` helpers (start, stop, wait with a timeout derived from
  `protocol/constants.py`, received-bytes from the DEBUG metrics lines).
- `conftest.py`: `make_mesh` fixture; stops every peer after the test.
- `test_sync.py`: create (static peers, UDP discovery, TLS), small edit (delta), delete,
  offline and rejoin, concurrent edit (conflict copy).

Marked `integration` and `slow`; plain `pytest -q` (and CI) runs them (about 15-25 s).
Run only these with `pytest -q -m integration`, skip them with `pytest -q -m "not integration"`.
On a timeout the assertion message holds the tail of every peer's log.

Owner: all members. Related issues: #62 (also #7, #40, #53, #55).
Rules: [`AGENTS.md`](../../AGENTS.md).
