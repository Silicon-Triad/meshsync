# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Multi-source chunk scheduler.

Owner: Huzaifa Hanif (CS-24039)
Contract (PROJECT_CONTEXT.md §8.3):
    Scheduler.fetch(entry, peers) -> completes or raises
    Rarest-first, round-robin tiebreak (D12); MAX_OUTSTANDING_PER_PEER;
    reassign outstanding requests on peer loss.

fetch() puts every chunk of `entry` that is missing from the store into the store, pulling from
all given peers in parallel (PROTOCOL.md §9.8). It does not write the file: the engine does that
with transfer.assembler.assemble (agreed on issue #14). It blocks until every chunk is stored,
or raises ConnectionError when some chunk has no peer left to ask. Chunks stored before the
error stay in the store, so the next attempt resumes from any peer.

Which peer has which chunk (issue #15, option (a)): every connected peer is a candidate for
every chunk. A peer is struck off for a chunk when it answers any ERROR (CHUNK_NOT_FOUND, BUSY) or
sends data that fails its SHA-256 check, and for all chunks when its connection fails. "Rarest"
is the number of candidates left, so chunks with fewer possible sources are asked for first;
ties keep file order, and peers are picked round-robin (D12).

Concurrent fetches (issue #60): Node may run two fetch() calls for the same entry at once (e.g.
the entry arrives in two peers' INDEX). Every chunk a fetch asks for is claimed in a map shared
by all fetches; another fetch that needs a claimed chunk waits for it instead of asking a peer
again. When the owner stores it, the waiter counts it as done; when the owner gives up, its
claims are released and the waiter asks for those chunks itself. An owner that gives up may
still have requests in flight; the waiter asks for those chunks again and the owner's late
replies are dropped, so a few chunks can travel twice in that (failure-only) case.

Threads: the calling thread does all bookkeeping and store.put. Reply futures complete on
connection reader threads; their callback only frees the peer's slot and queues the result.
There is no timeout here: a peer that stops answering is closed by the PING liveness check
(PROTOCOL.md §6.7), which fails its pending futures, and those chunks are reassigned.
"""

import logging
import queue
import threading
from collections import deque
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Protocol

from meshsync.net.connection import RemoteError
from meshsync.net.manager import PeerInfo
from meshsync.protocol.constants import MAX_OUTSTANDING_PER_PEER, MsgType

log = logging.getLogger(__name__)

_WAKE = object()  # queued to waiting fetches when a peer slot is freed


@dataclass(frozen=True)
class _Released:
    """Queued to other fetches when this fetch drops its claim on these chunks (#60)."""

    hashes: tuple[str, ...]


class ChunkSink(Protocol):
    """The part of sync.store.ChunkStore the scheduler needs."""

    def has(self, h: str) -> bool: ...

    def put(self, h: str, data: bytes) -> None: ...


class Scheduler:
    def __init__(self, store: ChunkSink) -> None:
        self.store = store
        # Outstanding CHUNK_REQ per peer, shared by every fetch() running at the same time, so
        # the §7.3 limit holds per peer and not per file. Guarded by _lock; no I/O under it.
        self._lock = threading.Lock()
        self._outstanding: dict[str, int] = {}
        self._waiters: set[queue.SimpleQueue] = set()
        # chunk hash -> the event queue of the fetch() that is getting it (#60). Guarded by _lock.
        self._claims: dict[str, queue.SimpleQueue] = {}

    # ------------------------------------------------------------ public (§8.3)

    def fetch(self, entry: dict, peers: Sequence[PeerInfo]) -> None:
        """Get every missing chunk of entry into the store, or raise ConnectionError."""
        order = {h: i for i, h in enumerate(dict.fromkeys(entry["chunks"]))}
        missing = [h for h in order if not self.store.has(h)]  # delta sync: only what's missing
        if not missing:
            return
        by_id = {p.peer_id: p for p in peers if not p.conn.closed}
        rotation = sorted(by_id)
        if not rotation:
            raise ConnectionError(f"{entry['path']}: no connected peer to fetch from")
        log.info(
            "fetching %s: %d of %d chunks from %d peers",
            entry["path"],
            len(missing),
            len(order),
            len(rotation),
        )

        # Chunks not yet asked for wait in `fresh` (file order). A chunk that failed and must be
        # asked again moves to `retry`. Fresh chunks have every live peer as a candidate, so a
        # retry chunk is never less rare than a fresh one: scanning retry (sorted) and then fresh
        # (in order) is rarest-first without re-sorting every chunk on every reply (D12).
        fresh: deque[str] = deque(missing)
        retry: set[str] = set()
        struck: dict[str, set[str]] = {h: set() for h in missing}  # chunk -> peers not to ask
        dead: set[str] = set()
        inflight: dict[str, str] = {}  # chunk -> peer it was asked from
        borrowed: set[str] = set()  # chunks another concurrent fetch is getting (#60)
        events: queue.SimpleQueue = queue.SimpleQueue()
        rr = 0  # round-robin position in rotation

        def candidates(h: str) -> list[str]:
            return [p for p in rotation if p not in dead and p not in struck[h]]

        def place(h: str) -> bool:
            """Ask a peer for h (or drop it if already stored). False if no candidate is free."""
            nonlocal rr
            if self.store.has(h):  # another fetch stored it meanwhile
                return True
            if self._claimed_by_other(h, events):
                borrowed.add(h)
                return True
            while True:
                cands = candidates(h)
                if not cands:
                    raise ConnectionError(f"{entry['path']}: no peer left for chunk {h}")
                for k in range(len(rotation)):  # round-robin from rr (D12)
                    pid = rotation[(rr + k) % len(rotation)]
                    if pid in cands and self._take_slot(pid):
                        rr = (rr + k + 1) % len(rotation)
                        break
                else:
                    return False
                # #60: claim only now that a request will go out, so a chunk we cannot ask for
                # yet never makes another fetch wait. Checked again under the lock: another
                # fetch may have claimed h since the check above.
                if not self._claim(h, events):
                    self._release_slot(pid)
                    borrowed.add(h)
                    return True
                if self._request(by_id[pid], h, events):
                    inflight[h] = pid
                    return True
                dead.add(pid)  # connection already gone: try the next candidate

        with self._lock:
            self._waiters.add(events)
        try:
            while fresh or retry or inflight or borrowed:
                # Rarest first: retried chunks by candidates left, ties in file order.
                for h in sorted(retry, key=lambda c: (len(candidates(c)), order[c])):
                    if place(h):
                        retry.discard(h)
                # Then fresh chunks in file order. They all share the same candidates (every
                # live peer), so once the head finds no free slot, none of the rest would.
                while fresh and place(fresh[0]):
                    fresh.popleft()
                if not (fresh or retry or inflight or borrowed):
                    break
                # Something is inflight (its reply or failure will arrive), or every free slot
                # is held by another fetch(), whose release queues a _WAKE here, or we wait for
                # borrowed chunks, whose owner queues a _Released here when it drops the claim.
                event = events.get()
                if isinstance(event, _Released):
                    # Only the chunks named in the event are checked: rescanning every borrowed
                    # chunk on every wake-up was quadratic (about 90 s for 1,400 chunks).
                    for h in borrowed.intersection(event.hashes):
                        borrowed.discard(h)
                        if not self.store.has(h):  # its owner gave up: ours to get
                            retry.add(h)
                    continue
                self._handle(event, events, retry, inflight, struck, dead)
        finally:
            self._release_claims(events)
            with self._lock:
                self._waiters.discard(events)
        log.info("fetched all chunks of %s", entry["path"])

    # ------------------------------------------------------------ internals

    def _request(self, peer: PeerInfo, h: str, events: queue.SimpleQueue) -> bool:
        """Send CHUNK_REQ; False (slot freed) if the connection is already gone."""
        try:
            fut = peer.conn.request(MsgType.CHUNK_REQ, {"hash": h})  # §6.4
        except (ConnectionError, OSError) as e:
            log.info("CHUNK_REQ to %s failed: %s", peer.peer_id, e)
            self._release_slot(peer.peer_id)
            return False
        fut.add_done_callback(lambda f, pid=peer.peer_id: self._done(events, pid, h, f))
        return True

    def _done(self, events: queue.SimpleQueue, pid: str, h: str, fut: Future) -> None:
        # Runs on a connection reader thread (or the closing thread): no I/O, never raises.
        self._release_slot(pid)
        events.put((pid, h, fut))

    def _handle(
        self,
        event: object,
        events: queue.SimpleQueue,
        retry: set[str],
        inflight: dict[str, str],
        struck: dict[str, set[str]],
        dead: set[str],
    ) -> None:
        if event is _WAKE:
            return
        pid, h, fut = event  # type: ignore[misc]
        del inflight[h]
        try:
            msg_type, body = fut.result()
        except RemoteError as e:  # ERROR reply, e.g. CHUNK_NOT_FOUND (non-fatal, §6.8)
            log.info("peer %s has no chunk %s (%s); asking another peer", pid, h, e.code.name)
            struck[h].add(pid)
            retry.add(h)
            return
        except (ConnectionError, OSError) as e:  # peer lost: reassign its chunks (§7.3)
            if pid not in dead:
                log.warning("peer %s lost during fetch (%s); reassigning its chunks", pid, e)
            dead.add(pid)
            retry.add(h)
            return
        try:
            if msg_type != MsgType.CHUNK_DATA or not isinstance(body, bytes):
                raise ValueError(f"expected CHUNK_DATA, got {msg_type!r}")
            self.store.put(h, body)  # verifies SHA-256 (§6.4, PROTOCOL.md §5.9)
            self._release_claims(events, [h])  # stored: fetches waiting on it are done with it
        except ValueError as e:
            # CHUNK_CORRUPT (PROTOCOL.md §5.9, §10): discard, ask another peer.
            log.warning("CHUNK_CORRUPT: chunk %s from %s rejected: %s", h, pid, e)
            struck[h].add(pid)
            retry.add(h)

    def _claim(self, h: str, events: queue.SimpleQueue) -> bool:
        """Claim h for this fetch; False if another fetch holds it."""
        with self._lock:
            owner = self._claims.setdefault(h, events)
        return owner is events

    def _claimed_by_other(self, h: str, events: queue.SimpleQueue) -> bool:
        with self._lock:
            owner = self._claims.get(h)
        return owner is not None and owner is not events

    def _release_claims(self, events: queue.SimpleQueue, hashes: list[str] | None = None) -> None:
        """Drop this fetch's claims (all, or the given ones) and tell the other fetches which."""
        with self._lock:
            keys = list(self._claims) if hashes is None else hashes
            released = tuple(h for h in keys if self._claims.get(h) is events)
            for h in released:
                del self._claims[h]
            waiters = [q for q in self._waiters if q is not events]
        if released:
            event = _Released(released)
            for q in waiters:  # SimpleQueue.put never blocks
                q.put(event)

    def _take_slot(self, pid: str) -> bool:
        with self._lock:
            n = self._outstanding.get(pid, 0)
            if n >= MAX_OUTSTANDING_PER_PEER:  # §7.3: at most 8 outstanding per peer
                return False
            self._outstanding[pid] = n + 1
            return True

    def _release_slot(self, pid: str) -> None:
        with self._lock:
            n = self._outstanding.get(pid, 0) - 1
            if n > 0:
                self._outstanding[pid] = n
            else:
                self._outstanding.pop(pid, None)
            waiters = list(self._waiters)
        for q in waiters:  # SimpleQueue.put never blocks
            q.put(_WAKE)
