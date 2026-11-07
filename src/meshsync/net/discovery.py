"""UDP discovery: ANNOUNCE/LEAVE broadcast and peer table (§6.1, §6.2, D11).

Owner: Muhammad Ahmed Qazi (CS-24045). Issue #1.
Contract (PROJECT_CONTEXT.md §8.3):
    Send ANNOUNCE every ANNOUNCE_INTERVAL, LEAVE on shutdown (§6.2).
    tag = HMAC-SHA256(K, canonical_json_without_tag) (D11); drop on bad tag or skew > 30 s.
    Decision (b): also drop ts <= last accepted ts for that peer_id (replay).
    Peer table with PEER_EXPIRY expiry.

Where our own peer_id comes from (generated, persisted, configured) is decided in the wiring
issue, not here: this module just takes it as an argument.

Timestamps are wall clock, int(time.time()), because D11 compares them across hosts for
freshness only. They are never used for conflict decisions (D2).
"""

import hashlib
import hmac
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from meshsync.protocol.constants import (
    ANNOUNCE_INTERVAL,
    ANNOUNCE_MAX_SKEW,
    MAGIC,
    MAX_DATAGRAM,
    PEER_EXPIRY,
    UDP_PORT,
    VERSION,
)
from meshsync.protocol.messages import canonical_json, decode_announce, encode_announce

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerIdentity:
    """Who we are. Minimal local copy until net/auth.py defines the shared one (dedupe later)."""

    peer_id: str
    name: str


@dataclass
class PeerInfo:
    """One row of the peer table."""

    peer_id: str
    name: str
    addr: str  # source IP of the datagram, never a value from the body
    tcp_port: int
    index_rev: int
    last_seen: float  # our local time when we last accepted a datagram from this peer
    last_ts: int  # sender's ts of the last accepted datagram (replay check, decision b)


def make_tag(key: bytes, fields: dict) -> str:
    """tag = HMAC-SHA256(K, canonical_json_without_tag), as hex (§6.2, D11)."""
    body = {k: v for k, v in fields.items() if k != "tag"}
    return hmac.new(key, canonical_json(body), hashlib.sha256).hexdigest()


def build_announce(
    key: bytes,
    me: PeerIdentity,
    tcp_port: int,
    index_rev: int,
    now: int,
    type: str = "ANNOUNCE",  # noqa: A002 - matches the §6.2 field name
) -> bytes:
    """Build a signed ANNOUNCE or LEAVE datagram (§6.2)."""
    fields = {
        "magic": MAGIC.decode("ascii"),
        "ver": VERSION,
        "type": type,
        "peer_id": me.peer_id,
        "name": me.name,
        "tcp_port": tcp_port,
        "index_rev": index_rev,
        "ts": now,
    }
    fields["tag"] = make_tag(key, fields)
    return encode_announce(fields)


def next_ts(now: float, last_sent: int) -> int:
    """ts for our next datagram: wall-clock seconds, but always > the last ts we sent.

    ts has 1 s resolution, and receivers drop ts <= the last accepted one (decision b). Without
    this, a LEAVE sent in the same second as our last ANNOUNCE would be dropped as a replay.
    """
    return max(int(now), last_sent + 1)


class PeerTable:
    """Peers we have heard from. Thread-safe: every method takes the lock."""

    def __init__(self, key: bytes, my_peer_id: str) -> None:
        self._key = key
        self._my_peer_id = my_peer_id
        self._peers: dict[str, PeerInfo] = {}
        # Last accepted ts per peer_id. Kept after LEAVE/expiry so an old datagram cannot be
        # replayed to bring a departed peer back (decision b).
        self._last_ts: dict[str, int] = {}
        self._lock = threading.Lock()

    def handle_datagram(self, data: bytes, src_ip: str, now: float) -> str | None:
        """Apply one received datagram. Returns "up", "update", "down" or None (dropped)."""
        result = self.apply(data, src_ip, now)
        return result[0] if result else None

    def apply(self, data: bytes, src_ip: str, now: float) -> tuple[str, PeerInfo] | None:
        """Like handle_datagram, but also returns a copy of the affected peer's row.

        Drop rules, in order (PROTOCOL.md §2): malformed, own peer_id, bad tag, stale ts, replay.
        """
        try:
            fields = decode_announce(data)  # rule 1: shape (§6.2)
        except ValueError as e:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            log.debug("drop from %s: malformed (%s)", src_ip, e)
            return None
        peer_id = fields["peer_id"]
        if peer_id == self._my_peer_id:  # rule 2: our own broadcast echoed back
            return None
        if not hmac.compare_digest(fields["tag"], make_tag(self._key, fields)):  # rule 3, D11
            log.warning("drop %s from %s: bad tag", peer_id, src_ip)
            return None
        ts = fields["ts"]
        if abs(now - ts) > ANNOUNCE_MAX_SKEW:  # rule 4, §6.2
            log.warning("drop %s from %s: stale ts %d", peer_id, src_ip, ts)
            return None
        with self._lock:
            if ts <= self._last_ts.get(peer_id, -1):  # rule 5, decision (b)
                log.warning("drop %s from %s: replay ts %d", peer_id, src_ip, ts)
                return None
            self._last_ts[peer_id] = ts
            if fields["type"] == "LEAVE":  # rule 6
                gone = self._peers.pop(peer_id, None)
                return ("down", gone) if gone else None
            peer = self._peers.get(peer_id)  # rule 7: ANNOUNCE
            if peer is None:
                self._peers[peer_id] = PeerInfo(
                    peer_id=peer_id,
                    name=fields["name"],
                    addr=src_ip,
                    tcp_port=fields["tcp_port"],
                    index_rev=fields["index_rev"],
                    last_seen=now,
                    last_ts=ts,
                )
                return "up", replace(self._peers[peer_id])
            peer.name = fields["name"]
            peer.addr = src_ip
            peer.tcp_port = fields["tcp_port"]
            peer.index_rev = fields["index_rev"]
            peer.last_seen = now
            peer.last_ts = ts
            return "update", replace(peer)

    def expire(self, now: float) -> list[PeerInfo]:
        """Remove and return peers not heard from for more than PEER_EXPIRY seconds (§6.2)."""
        with self._lock:
            gone = [p for p in self._peers.values() if now - p.last_seen > PEER_EXPIRY]
            for p in gone:
                del self._peers[p.peer_id]
        return gone

    def peers(self) -> list[PeerInfo]:
        """Snapshot of the table (copies, safe to read without the lock)."""
        with self._lock:
            return [replace(p) for p in self._peers.values()]


class Discovery:
    """Broadcasts our ANNOUNCE and listens for others'. Two daemon threads (D1)."""

    def __init__(
        self,
        key: bytes,
        me: PeerIdentity,
        tcp_port: int,
        index_rev: Callable[[], int],
        on_event: Callable[[str, PeerInfo], None],
        udp_port: int = UDP_PORT,
        broadcast_addr: str = "255.255.255.255",
    ) -> None:
        self.table = PeerTable(key, me.peer_id)
        self._key = key
        self._me = me
        self._tcp_port = tcp_port
        self._index_rev = index_rev
        self._on_event = on_event
        self._dest = (broadcast_addr, udp_port)
        self._stop = threading.Event()
        self._last_sent_ts = 0  # see next_ts(); only the sender thread, then stop(), touch it
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # §14: several local peers share the UDP port, so allow address reuse.
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.settimeout(1.0)  # recvfrom wakes up every second so stop() is responsive
        self._sock.bind(("", udp_port))
        self._threads = [
            threading.Thread(target=self._send_loop, name="discovery-send", daemon=True),
            threading.Thread(target=self._listen_loop, name="discovery-listen", daemon=True),
        ]

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        """Send one LEAVE, stop both threads and close the socket."""
        self._stop.set()
        for t in self._threads:
            t.join()
        self._send("LEAVE")
        self._sock.close()

    def _send(self, type: str) -> None:  # noqa: A002 - matches the §6.2 field name
        self._last_sent_ts = next_ts(time.time(), self._last_sent_ts)
        data = build_announce(
            self._key, self._me, self._tcp_port, self._index_rev(), self._last_sent_ts, type
        )
        try:
            self._sock.sendto(data, self._dest)
        except OSError as e:  # e.g. network down; discovery is soft state, retry next tick
            log.warning("send %s failed: %s", type, e)

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            self._send("ANNOUNCE")
            self._stop.wait(ANNOUNCE_INTERVAL)

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, (src_ip, _port) = self._sock.recvfrom(
                    MAX_DATAGRAM + 1
                )  # +1 so oversize is seen
            except TimeoutError:
                data = None
            except OSError as e:
                log.warning("recvfrom failed: %s", e)
                data = None
            now = time.time()
            if data is not None:
                result = self.table.apply(data, src_ip, now)
                if result is not None:
                    self._emit(*result)
            for peer in self.table.expire(now):
                log.info("peer %s (%s) expired", peer.peer_id, peer.name)
                self._emit("down", peer)

    def _emit(self, event: str, peer: PeerInfo) -> None:
        # Called outside the table lock so a slow subscriber cannot block discovery.
        try:
            self._on_event(event, peer)
        except Exception:
            log.exception("on_event(%s, %s) failed", event, peer.peer_id)
