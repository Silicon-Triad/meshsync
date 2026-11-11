"""Node: wires PeerManager to the sync and transfer components (§6.4, §6.7, §7, §8.3).

Owner: Muhammad Ahmed Qazi (CS-24045). Issue #40. PROTOCOL.md decisions (k), (l).

Node knows the other members' modules only through the typing.Protocols below, so it can be
built and tested before they exist. `Action` is defined here; Mujtaba's engine imports it from
`meshsync.node`.

Flow:
  peer_up        -> INDEX_REQ to the peer, INDEX back, every entry goes to the engine, mark_synced
  NOTIFY         -> the entry goes to the engine
  engine "fetch" -> scheduler.fetch(entry, peers), then engine.on_fetched(entry)
  scanner change -> NOTIFY {entry} to every peer
Handlers run on connection reader threads, so they only reply or hand work to a thread pool.
"""

import logging
import re
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from meshsync.config import Config
from meshsync.net import auth, tls
from meshsync.net.auth import PeerIdentity
from meshsync.net.connection import Connection
from meshsync.net.manager import PeerInfo, PeerManager
from meshsync.protocol.constants import HANDSHAKE_TIMEOUT, META_DIR, ErrorCode, MsgType

log = logging.getLogger(__name__)

FETCH_WORKERS = 4  # PROTOCOL.md decision (k)
_PEER_ID_RE = re.compile(r"[0-9A-F]{8}")


@dataclass(frozen=True)
class Action:
    """What the engine decides about a remote entry (§7). Imported by sync/engine.py."""

    kind: Literal["fetch", "skip", "conflict", "delete"]
    entry: dict


class IndexLike(Protocol):
    """Implemented by sync/index.py (Mujtaba)."""

    @property
    def rev(self) -> int:
        """Bumps on every put. Sent in discovery ANNOUNCE and in INDEX."""
        ...

    def get(self, path: str) -> dict | None: ...

    def put(self, entry: dict) -> None: ...

    def entries(self) -> list[dict]:
        """All entries. Node calls it when a peer sends INDEX_REQ."""
        ...


class StoreLike(Protocol):
    """Implemented by sync/store.py (Mujtaba)."""

    def has(self, h: str) -> bool: ...

    def get(self, h: str) -> bytes:
        """Node calls it for CHUNK_REQ. Must raise KeyError or FileNotFoundError if absent."""
        ...

    def put(self, h: str, data: bytes) -> None: ...


class EngineLike(Protocol):
    """Implemented by sync/engine.py (Mujtaba)."""

    def on_remote_entry(self, peer_id: str, entry: dict) -> Action:
        """Node calls it for every entry from INDEX or NOTIFY."""
        ...

    def apply(self, action: Action) -> None:
        """Node calls it for "conflict" and "delete" actions (side effects on disk and index)."""
        ...

    def on_fetched(self, entry: dict) -> None:
        """Node calls it after scheduler.fetch(entry) succeeded."""
        ...


class SchedulerLike(Protocol):
    """Implemented by transfer/scheduler.py (Huzaifa)."""

    def fetch(self, entry: dict, peers: list[PeerInfo]) -> None:
        """Node calls it on a worker thread. Blocks until the file is done, or raises."""
        ...


class ScannerLike(Protocol):
    """Implemented by sync/scanner.py (Mujtaba)."""

    def start(self, on_change: Callable[[dict], None]) -> None:
        """on_change gets the new or updated local entry, already stored in the index."""
        ...

    def stop(self) -> None: ...


def load_or_create_peer_id(sync_dir: Any) -> str:
    """Own peer_id: 8 uppercase hex chars, kept in <sync_dir>/.meshsync/peer_id (decision (l))."""
    path = sync_dir / META_DIR / "peer_id"
    if path.exists():
        peer_id = path.read_text().strip()
        if _PEER_ID_RE.fullmatch(peer_id):
            return peer_id
        log.warning("ignoring invalid peer_id in %s", path)
    peer_id = secrets.token_hex(4).upper()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(peer_id + "\n")
    return peer_id


class Node:
    def __init__(
        self,
        config: Config,
        *,
        index: IndexLike,
        store: StoreLike,
        engine: EngineLike,
        scheduler: SchedulerLike,
        scanner: ScannerLike,
        manager: PeerManager | None = None,
    ) -> None:
        self.config = config
        self.index = index
        self.store = store
        self.engine = engine
        self.scheduler = scheduler
        self.scanner = scanner
        self.manager = manager or self._build_manager()
        self._pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fetch")
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False

    def _build_manager(self) -> PeerManager:
        cfg = self.config
        me = PeerIdentity(load_or_create_peer_id(cfg.sync_dir), cfg.name)
        server_ctx = client_ctx = None
        if cfg.tls:
            assert cfg.cert_dir is not None  # load_config guarantees it with --tls
            server_ctx = tls.server_context(cfg.cert_dir, cfg.name)
            client_ctx = tls.client_context(cfg.cert_dir, cfg.name)
        return PeerManager(
            auth.load_secret(cfg.secret_file),
            me,
            tcp_port=cfg.tcp_port,
            udp_port=cfg.udp_port,
            tls_ctx_server=server_ctx,
            tls_ctx_client=client_ctx,
            static_peers=list(cfg.peers),
            index_rev=lambda: self.index.rev,
        )

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        mgr = self.manager
        mgr.register_handler(MsgType.INDEX_REQ, self._on_index_req)
        mgr.register_handler(MsgType.NOTIFY, self._on_notify)
        mgr.register_handler(MsgType.CHUNK_REQ, self._on_chunk_req)
        mgr.on("peer_up", self._on_peer_up)
        mgr.start()
        self.scanner.start(self._on_local_change)

    def stop(self) -> None:
        with self._lock:
            if self._stopped or not self._started:
                return
            self._stopped = True
        self.scanner.stop()
        self.manager.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ handlers (reader threads)

    def _on_index_req(self, conn: Connection, req_id: int, body: Any) -> None:
        conn.reply(
            req_id,
            MsgType.INDEX,
            {"index_rev": self.index.rev, "files": self.index.entries()},
        )

    def _on_notify(self, conn: Connection, req_id: int, body: Any) -> None:
        # decode_body already checks this (fatal BAD_HEADER); stay safe if a caller skips it.
        entry = body.get("entry") if isinstance(body, dict) else None
        if not isinstance(entry, dict):
            log.warning("dropping malformed NOTIFY from %s", conn.remote.peer_id)
            return
        self._handle_remote(conn.remote.peer_id, entry)

    def _on_chunk_req(self, conn: Connection, req_id: int, body: Any) -> None:
        try:
            data = self.store.get(body["hash"])
        except (KeyError, FileNotFoundError):
            conn.reply(
                req_id,
                MsgType.ERROR,
                {"code": int(ErrorCode.CHUNK_NOT_FOUND), "msg": "chunk not found"},
            )
            return
        conn.reply(req_id, MsgType.CHUNK_DATA, data)

    # ------------------------------------------------------------ peer_up, remote entries

    def _on_peer_up(self, peer: PeerInfo) -> None:
        # Runs on a reader/dialer thread: do the request/wait on a worker thread (§9.1).
        threading.Thread(
            target=self._exchange_index, args=(peer,), name=f"index-{peer.peer_id}", daemon=True
        ).start()

    def _exchange_index(self, peer: PeerInfo) -> None:
        # 10 s (HANDSHAKE_TIMEOUT) is enough: INDEX is at most MAX_BODY (1 MiB) on the wire.
        # If the exchange fails the link must not stay AUTHENTICATED forever, so it is marked
        # SYNCED anyway; NOTIFY keeps delivering changes and the next reconnect retries INDEX.
        try:
            msg_type, body = peer.conn.request(MsgType.INDEX_REQ, {}).result(HANDSHAKE_TIMEOUT)
            if msg_type != MsgType.INDEX:
                raise ValueError(f"expected INDEX, got {msg_type!r}")
            for entry in body["files"]:
                self._handle_remote(peer.peer_id, entry)
        except Exception:  # noqa: BLE001 - any failure here only affects this one peer
            log.warning("index exchange with %s failed", peer.peer_id, exc_info=True)
        finally:
            peer.conn.mark_synced()  # no-op unless the link is still AUTHENTICATED

    def _handle_remote(self, peer_id: str, entry: dict) -> None:
        if self._stopped:
            return
        action = self.engine.on_remote_entry(peer_id, entry)
        try:
            if action.kind == "fetch":
                self._pool.submit(self._fetch, action.entry)
            elif action.kind == "conflict":
                # §7.2, PROTOCOL.md §9.4: the conflict copy needs the remote chunks first
                self._pool.submit(self._fetch_then_apply, action)
            elif action.kind == "delete":  # a tombstone has no chunks (§9.5)
                self._pool.submit(self._apply, action)
            # "skip": nothing to do
        except RuntimeError:  # pool shut down by stop() while this reader thread was running
            log.debug("dropping %s for %s: node is stopping", action.kind, entry.get("path"))

    def _fetch(self, entry: dict) -> None:
        try:
            self.scheduler.fetch(entry, self.manager.peers())
            self.engine.on_fetched(entry)
        except Exception:  # noqa: BLE001 - logged; a failed fetch must not kill the worker
            log.exception("fetch of %s failed", entry.get("path"))

    def _fetch_then_apply(self, action: Action) -> None:
        try:
            self.scheduler.fetch(action.entry, self.manager.peers())
        except Exception:  # noqa: BLE001 - logged; the next NOTIFY or INDEX exchange retries
            log.exception("fetch for %s of %s failed", action.kind, action.entry.get("path"))
            return
        self._apply(action)

    def _apply(self, action: Action) -> None:
        try:
            self.engine.apply(action)
        except Exception:  # noqa: BLE001
            log.exception("applying %s for %s failed", action.kind, action.entry.get("path"))

    # ------------------------------------------------------------ local changes

    def _on_local_change(self, entry: dict) -> None:
        for peer in self.manager.peers():
            try:
                peer.conn.send(MsgType.NOTIFY, {"entry": entry})
            except Exception:  # noqa: BLE001
                log.warning("NOTIFY to %s failed", peer.peer_id, exc_info=True)


class ComponentsMissing(Exception):
    """The sync/transfer modules are not implemented yet."""


def build_node(config: Config) -> Node:
    """Build a Node from the real sync/transfer classes (names per PROJECT_CONTEXT.md §8.3)."""
    try:
        from meshsync.sync.engine import Engine
        from meshsync.sync.index import Index
        from meshsync.sync.scanner import Scanner
        from meshsync.sync.store import ChunkStore
        from meshsync.transfer.scheduler import Scheduler

        index = Index(config.sync_dir)
        store = ChunkStore(config.sync_dir)
        engine = Engine(config, index, store)
        scheduler = Scheduler(store)
        scanner = Scanner(config, index)
    except (ImportError, NotImplementedError) as e:
        raise ComponentsMissing(str(e)) from e
    node = Node(
        config, index=index, store=store, engine=engine, scheduler=scheduler, scanner=scanner
    )
    engine.on_change = node._on_local_change  # issue #14 point 3: resolved conflicts -> NOTIFY
    engine.lock = scanner.lock = threading.RLock()  # #14: one lock for engine and scanner
    return node
