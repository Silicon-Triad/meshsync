# AI Use Log

Record of every AI-assisted contribution, for the report's AI-use appendix (PROJECT_CONTEXT.md §16).

| Date | Member | Tool | Used for | Kept / changed |
| ---- | ------ | ---- | -------- | -------------- |
| 2026-11-02 | Muhammad Ahmed Qazi | Claude Code | Repository scaffold from the scaffold brief: pyproject, CI, PR template, PROTOCOL.md, constants.py, framing.py + unit tests, module stubs, CLI skeleton, gen_certs.sh | Kept as generated; to be reviewed line by line by another member before merge (PR #27) |
| 2026-11-02 | Muhammad Ahmed Qazi | Claude Code | GitHub setup guide (docs/setup/github.md), CODEOWNERS, CONTRIBUTING.md from the team settings brief | Kept as generated; to be reviewed by another member before merge (PR #28) |
| 2026-11-02 | Muhammad Ahmed Qazi | Claude Code | AGENTS.md rulebook, CLAUDE.md, folder READMEs, FROZEN INTERFACE headers, scripts/check.sh, CONTRIBUTING.md, PR template (#29) | Draft reviewed and adjusted by Ahmed (branch names, PR size rule); to be reviewed by another member before merge |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code (Opus 5.5) | TLS contexts (#5): net/tls.py, tests/unit/test_tls.py, PROTOCOL.md §6, README | |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code | messages.py scaffold + tests (#31) | Reviewed messages.py and tests line by line; decided review round 1 (decision label (c), tombstone shape (f), kept branch name); fixes from Mujtaba's review (RecursionError, path control chars, allow_nan, hex case (d)) applied via Claude |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code | CLI + config (#6): config.py, __main__.py, tests/unit/test_config.py, README config example | |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code | UDP discovery (#1): net/discovery.py, tests/unit/test_discovery.py, PROTOCOL.md §2 drop rules | <kept/changed> |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code | HMAC handshake (net/auth.py, tests, PROTOCOL.md decision g) (#4) | |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code (Sonnet 5.5) | Connection (#2): net/connection.py, tests/unit/test_connection.py, tests/conftest.py (shared cert fixtures), PeerIdentity dedupe, PROTOCOL.md §3/§6/§7 + decisions (h), (i) | |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code (Sonnet 5.5) | PeerManager (#3): net/manager.py, tests/unit/test_manager.py, PROTOCOL.md §8a + decision (j) | |
| 2026-11-08 | Muhammad Ahmed Qazi | Claude Code (Sonnet 5.5) | node wiring (#40): node.py, __main__ run, tests/fakes.py, tests/unit/test_node.py, PROTOCOL.md §8b + decisions (k), (l) | |
| 2026-11-08 | Muhammad Ahmed Qazi | Claude Code (Opus 5.5) | local mesh script (#7): scripts/run_local_mesh.sh, .gitignore, README local testing | |
| 2026-11-08 | Muhammad Ahmed Qazi | Claude Code (Opus 5.5) | PROTOCOL.md consolidation (#8): rewrite as one spec, worked examples generated from the code | |
| 2026-11-08 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | version vectors (#13): sync/vclock.py, tests/unit/test_vclock.py | |
| 2026-11-08 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | chunk store (#11): sync/store.py, tests/unit/test_store.py | |
| 2026-11-10 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | chunker (#9): sync/chunker.py, tests/unit/test_chunker.py | |
| 2026-11-10 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | local index (#10): sync/index.py, tests/unit/test_index.py | |
| 2026-11-11 | Muhammad Ahmed Qazi | Claude Code (Opus 5.5) | conflict fetch before apply (#49): node.py, tests/fakes.py, tests/unit/test_node.py | |
| 2026-11-10 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | polling scanner (#12): sync/scanner.py, tests/unit/test_scanner.py | |
| 2026-11-11 | Huzaifa Hanif | Claude Code (Opus 5.5) | file assembler (#16): transfer/assembler.py, tests/unit/test_assembler.py | |
| 2026-11-11 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | persisted scan cache (#51): sync/scanner.py, tests/unit/test_scanner.py | |
| 2026-11-11 | Huzaifa Hanif | Claude Code (Opus 5.5) | chunk scheduler (#15): transfer/scheduler.py, tests/unit/test_scheduler.py | |
| 2026-11-11 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | sync engine (#14): sync/engine.py, tests/unit/test_engine.py, build_node on_change line, PROTOCOL.md §9.4 conflict rules | |
| 2026-11-11 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | netem profiles (#18): scripts/netem.sh, tests/unit/test_netem.py | |
| 2026-11-11 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | test data generator (#19): scripts/gen_testdata.py, tests/unit/test_gen_testdata.py; data set proposal on #25 | |
| 2026-11-11 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | per-message metrics logger (#17): metrics/logger.py, record calls in net/connection.py, tests/unit/test_logger.py, test_connection.py | |
| 2026-11-12 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | integration tests (#62): tests/integration/{mesh.py,conftest.py,test_sync.py,README.md}, integration marker in pyproject.toml | |
| 2026-11-12 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | §10 failure-scenario integration tests (#63): tests/integration/test_failures.py, mesh.py additions (kill, wait_log, per-peer secret/cert dir/plain), README | |
| 2026-11-12 | Huzaifa Hanif | Claude Code (Opus 5.5) | measurement runner, §11 conditions 1-3 (#20): scripts/measure/run.py, peer.py, tests/unit/test_measure.py | |
| 2026-11-12 | Huzaifa Hanif | Claude Code (Opus 5.5) | dedupe concurrent fetches (#60): transfer/scheduler.py, tests/unit/test_scheduler.py | |
| 2026-11-12 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | §11 conditions 5-6 runners (#69): impaired and tls experiments, setup_s column, netem helpers in scripts/measure/run.py; tests in tests/unit/test_measure.py; README | |
| 2026-11-12 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | analysis script (#71): analysis/analyze.py (tables, mean ± sd, §13 savings, matplotlib graphs), tests/unit/test_analyze.py, analysis extra in pyproject.toml, README | |
| 2026-11-14 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | network baseline (#70): scripts/measure/baseline.{sh,py}, tests/unit/test_baseline.py, step-by-step commands for the 3-host session, data/baseline/README.md written from the measured data, reuse-vs-reconnect derivation | |
| 2026-11-12 | Mujtaba Jawaid Rao | Claude Code (Opus 5.5) | 3-host deployment session (#67): step-by-step commands for the run, data/deploy/README.md written from the captured logs and host info; redaction of hostname, user name and MACs | |
| 2026-11-14 | Muhammad Ahmed Qazi | Claude Code (Opus 5.5) | `meshsync run --metrics-csv PATH` / `metrics_csv` key (#72): __main__.py, config.py, tests/unit/test_config.py, README config section | Kept; design decided by Ahmed on #72, plan approved before code; pending review by Mujtaba |
