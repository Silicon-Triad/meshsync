# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""PeerManager: dialing, accept loop, liveness and reconnect (PROJECT_CONTEXT.md §6.4, §6.7, §8.3).

Owner: Muhammad Ahmed Qazi (CS-24045). Issue #3.

    mgr = PeerManager(key, me, tcp_port=47001, udp_port=47000, tls_ctx_server=None,
                      tls_ctx_client=None, static_peers=[], broadcast_addr="255.255.255.255")
    mgr.on("peer_up", lambda peer: ...)              # peer: PeerInfo
    mgr.on("peer_down", lambda peer, reason: ...)
    mgr.register_handler(MsgType.NOTIFY, handler)    # applied to every connection, now and later
    mgr.start(); mgr.peers(); mgr.get(peer_id); mgr.stop()

NOTE: `PeerInfo` here is the CONNECTED peer (it owns the Connection). It is not
`discovery.PeerInfo`, which is a row of the UDP peer table; that one is only used here as
`discovery.PeerInfo`.

Dial rule (§6.4): the lower peer_id dials. Peers given with --peer (§14) are dialled regardless,
because their peer_id is unknown until the handshake. Two links to the same peer can then exist;
we keep the one whose initiator has the lower peer_id (PROTOCOL.md decision (j)).

Threads (all daemon): accept loop, one short-lived thread per incoming handshake, one dialer per
target, one liveness thread, plus the two discovery threads. `_lock` guards the tables below;
no socket I/O and no user callback ever runs while it is held (§9.1). Event callbacks and
handlers run on whichever thread caused the event (often a Connection reader thread), so they
must be quick (see connection.py).
"""

import logging
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from ssl import SSLContext

from meshsync.net import discovery
from meshsync.net.auth import PeerIdentity
from meshsync.net.connection import Connection, Handler, establish
from meshsync.protocol.constants import (
    ANNOUNCE_INTERVAL,
    HANDSHAKE_TIMEOUT,
    MAX_MISSED_PONGS,
    PING_INTERVAL,
    MsgType,
)
from meshsync.protocol.framing import ProtocolError

log = logging.getLogger(__name__)

EVENTS = ("peer_up", "peer_down")
DialKey = str | tuple[str, int]  # a discovered peer_id, or a static (host, port)


@dataclass
class PeerInfo:
    """A connected, authenticated peer (§8.3)."""

    peer_id: str
    name: str
    addr: str
    tcp_port: int  # the peer's listening port; 0 if it dialled us and was never discovered
    conn: Connection
    rtt_ms: float | None = None  # last PING round trip
    missed_pongs: int = 0
    we_dialed: bool = False  # which side initiated this link (needed by the duplicate rule)


class PeerManager:
    def __init__(
        self,
        key: bytes,
        me: PeerIdentity,
        *,
        tcp_port: int,
        udp_port: int,
        tls_ctx_server: SSLContext | None = None,
        tls_ctx_client: SSLContext | None = None,
        static_peers: list[tuple[str, int]] | None = None,
        broadcast_addr: str = "255.255.255.255",
        discovery_factory: Callable[[Callable], "discovery.Discovery"] | None = None,
        index_rev: Callable[[], int] = lambda: 0,  # the wiring issue supplies the real one
    ) -> None:
        self._key = key
        self._me = me
        self._tcp_port = tcp_port
        self._tls_server = tls_ctx_server
        self._tls_client = tls_ctx_client
        self._static_peers = static_peers or []
        # Discovery takes its callback in its constructor, so tests inject a factory, not an object.
        self._discovery_factory = discovery_factory or (
            lambda on_event: discovery.Discovery(
                key, me, tcp_port, index_rev, on_event, udp_port, broadcast_addr
            )
        )
        self._mode = "tls" if tls_ctx_client or tls_ctx_server else "plain"
        self._lock = threading.Lock()
        self._peers: dict[str, PeerInfo] = {}  # connected peers, by peer_id
        self._seen: dict[str, discovery.PeerInfo] = {}  # peers currently in the UDP table
        self._dialing: set[DialKey] = set()  # targets that have a dialer thread
        self._handlers: dict[MsgType, Handler] = {}
        self._listeners: dict[str, list[Callable]] = {name: [] for name in EVENTS}
        self._stop = threading.Event()
        self._started = False
        self._listener: socket.socket | None = None
        self._discovery: discovery.Discovery | None = None
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ public API

    def on(self, event: str, fn: Callable) -> None:
        """event "peer_up": fn(PeerInfo); "peer_down": fn(PeerInfo, reason)."""
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}, expected one of {EVENTS}")
        with self._lock:
            self._listeners[event].append(fn)

    def register_handler(self, msg_type: MsgType, fn: Handler) -> None:
        """Handle `msg_type` on every connection, current and future (Connection.on)."""
        with self._lock:
            self._handlers[msg_type] = fn
            conns = [p.conn for p in self._peers.values()]
        for conn in conns:
            conn.on(msg_type, fn)

    def peers(self) -> list[PeerInfo]:
        """Snapshot of the connected peers (copies)."""
        with self._lock:
            return [replace(p) for p in self._peers.values()]

    def get(self, peer_id: str) -> PeerInfo | None:
        with self._lock:
            peer = self._peers.get(peer_id)
            return replace(peer) if peer else None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.settimeout(1.0)  # accept wakes every second so stop() is responsive
        self._listener.bind(("", self._tcp_port))
        self._listener.listen()
        for target, name in ((self._accept_loop, "accept"), (self._liveness_loop, "liveness")):
            thread = threading.Thread(target=target, name=f"manager-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self._discovery = self._discovery_factory(self._on_discovery)
        self._discovery.start()
        for host_port in self._static_peers:  # §14 fallback
            self._start_dialer(host_port)
        log.info(
            "peer manager started: %s on tcp %d (%s)", self._me.name, self._tcp_port, self._mode
        )

    def stop(self) -> None:
        """BYE to every peer, then close the listener and discovery. Safe to call twice."""
        if self._stop.is_set() or not self._started:
            return
        self._stop.set()
        with self._lock:
            conns = [p.conn for p in self._peers.values()]
        for conn in conns:
            conn.close("shutdown")  # sends BYE; our own peer_down fires from on_close
        if self._listener is not None:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)  # wakes accept() now, not in 1 s
            except OSError as e:
                log.debug("listener shutdown: %s", e)
            self._listener.close()
        if self._discovery is not None:
            self._discovery.stop()  # sends LEAVE
        for thread in self._threads:
            thread.join(timeout=2)
        log.info("peer manager stopped")

    # ------------------------------------------------------------ events

    def _fire(self, event: str, *args: object) -> None:
        with self._lock:
            listeners = list(self._listeners[event])
        for fn in listeners:  # outside the lock
            try:
                fn(*args)
            except Exception:
                log.exception("%s listener failed", event)

    # ------------------------------------------------------------ discovery and dialing

    def _on_discovery(self, event: str, info: "discovery.PeerInfo") -> None:
        """Dial rule (§6.4). "down" is ignored: only TCP liveness ends a working connection."""
        with self._lock:
            if event == "down":
                self._seen.pop(info.peer_id, None)  # lets its dialer give up
                return
            self._seen[info.peer_id] = info
            connected = info.peer_id in self._peers
        if not connected and self._me.peer_id < info.peer_id:
            self._start_dialer(info.peer_id)
        # else: the higher peer_id waits to be dialled

    def _start_dialer(self, key: DialKey) -> None:
        with self._lock:
            if key in self._dialing or self._stop.is_set():
                return
            self._dialing.add(key)
        thread = threading.Thread(target=self._dial_loop, args=(key,), name="manager-dial")
        thread.daemon = True
        thread.start()

    def _dial_loop(self, key: DialKey) -> None:
        """Try every ANNOUNCE_INTERVAL. A discovered peer stops once connected or gone from the
        UDP table (re-armed by _on_closed); a static peer retries for as long as we run."""
        learned_id: str | None = None  # a static peer's id, known after the first handshake
        while not self._stop.is_set():
            with self._lock:
                seen = self._seen.get(key) if isinstance(key, str) else None
                target = (seen.addr, seen.tcp_port) if seen else key
                connected = (key if isinstance(key, str) else learned_id) in self._peers
                if isinstance(key, str) and (seen is None or connected):
                    self._dialing.discard(key)  # decided under the lock: no lost re-arm
                    return
            if not connected and isinstance(target, tuple):
                learned_id = self._dial_once(target) or learned_id
            self._stop.wait(ANNOUNCE_INTERVAL)
        with self._lock:
            self._dialing.discard(key)

    def _dial_once(self, target: tuple[str, int]) -> str | None:
        """One connection attempt. Returns the peer_id on success, None on failure."""
        log.debug("dialing %s:%d", *target)
        try:
            sock = socket.create_connection(target, timeout=HANDSHAKE_TIMEOUT)
            conn = establish(sock, self._key, self._me, initiator=True, tls_ctx=self._tls_client)
        except (OSError, ProtocolError) as e:  # establish() has closed the socket
            log.debug("dial %s:%d failed: %s", *target, e)
            return None
        self._adopt(conn, target[0], target[1], we_dialed=True)
        return conn.remote.peer_id

    # ------------------------------------------------------------ accepting

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, (ip, _port) = self._listener.accept()
            except TimeoutError:
                continue
            except OSError as e:
                if not self._stop.is_set():
                    log.warning("accept failed: %s", e)
                    self._stop.wait(1)
                continue
            # A slow or hostile peer must not block accepting: handshake in its own thread.
            thread = threading.Thread(target=self._handshake_in, args=(sock, ip), daemon=True)
            thread.start()

    def _handshake_in(self, sock: socket.socket, ip: str) -> None:
        sock.settimeout(HANDSHAKE_TIMEOUT)
        try:
            conn = establish(sock, self._key, self._me, initiator=False, tls_ctx=self._tls_server)
        except (OSError, ProtocolError) as e:
            log.warning("handshake from %s failed: %s", ip, e)
            return
        self._adopt(conn, ip, 0, we_dialed=False)

    # ------------------------------------------------------------ connection lifecycle

    def _adopt(self, conn: Connection, addr: str, tcp_port: int, *, we_dialed: bool) -> None:
        """Register an authenticated Connection, applying the duplicate rule (decision (j))."""
        remote = conn.remote
        if remote.peer_id == self._me.peer_id:  # also refused by the handshake (auth.py)
            conn.close("self-connection")
            return
        lower_dials = self._me.peer_id < remote.peer_id  # True: the lower id is us
        preferred = we_dialed == lower_dials  # the link whose initiator has the lower peer_id
        reject = ""  # why the new link is refused; closed after the lock (BYE is socket I/O)
        old = None
        with self._lock:
            if self._stop.is_set():
                reject = "shutdown"
            else:
                old = self._peers.get(remote.peer_id)
                if old is not None and old.we_dialed == lower_dials and not preferred:
                    reject = "duplicate"  # the existing link is the preferred one
                else:
                    seen = self._seen.get(remote.peer_id)
                    port = tcp_port or (seen.tcp_port if seen else 0)
                    info = PeerInfo(
                        remote.peer_id, remote.name, addr, port, conn, we_dialed=we_dialed
                    )
                    for msg_type, fn in self._handlers.items():  # before start(): none missed
                        conn.on(msg_type, fn)
                    self._peers[remote.peer_id] = info
        if reject:
            conn.close(reject)
            return
        if old is not None:  # replaced: tell consumers the old link is gone
            old.conn.close("duplicate")
            self._fire("peer_down", old, "duplicate")
        conn.on_close(self._on_closed)
        conn.start()
        log.info("peer up: %s %s:%d (%s)", info.name, addr, info.tcp_port, self._mode)
        self._fire("peer_up", replace(info))

    def _on_closed(self, conn: Connection, reason: str) -> None:
        with self._lock:
            info = self._peers.get(conn.remote.peer_id)
            if info is None or info.conn is not conn:  # already replaced by a newer link
                return
            del self._peers[info.peer_id]
            redial = info.peer_id in self._seen and self._me.peer_id < info.peer_id
        log.info("peer down: %s %s (%s): %s", info.name, info.addr, self._mode, reason)
        self._fire("peer_down", info, reason)
        if redial:  # reconnect: still announcing on UDP
            self._start_dialer(info.peer_id)

    # ------------------------------------------------------------ liveness (§6.7, D13)

    def _liveness_loop(self) -> None:
        pings: dict[str, Future] = {}  # last PING per peer; only this thread touches it
        while not self._stop.wait(PING_INTERVAL):
            for peer in self.peers():
                last = pings.get(peer.peer_id)
                if last is not None and not last.done():  # the previous PING got no PONG
                    if self._count_miss(peer) >= MAX_MISSED_PONGS:
                        peer.conn.close("liveness")
                        continue
                try:
                    fut = peer.conn.request(MsgType.PING, {"t": time.time()})
                except ConnectionError:
                    continue  # closing; _on_closed does the rest
                started = time.monotonic()
                fut.add_done_callback(lambda f, p=peer, t=started: self._on_pong(p, t, f))
                pings[peer.peer_id] = fut

    def _count_miss(self, peer: PeerInfo) -> int:
        with self._lock:
            live = self._peers.get(peer.peer_id)
            if live is None or live.conn is not peer.conn:
                return 0
            live.missed_pongs += 1
            return live.missed_pongs

    def _on_pong(self, peer: PeerInfo, started: float, fut: Future) -> None:
        """Runs on the reader thread when the PONG arrives (or when the connection fails)."""
        if fut.exception() is not None:
            return
        rtt_ms = (time.monotonic() - started) * 1000
        with self._lock:
            live = self._peers.get(peer.peer_id)
            if live is not None and live.conn is peer.conn:
                live.rtt_ms = rtt_ms
                live.missed_pongs = 0
