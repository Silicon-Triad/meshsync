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
from typing import Protocol

from meshsync.net.connection import RemoteError
from meshsync.net.manager import PeerInfo
from meshsync.protocol.constants import MAX_OUTSTANDING_PER_PEER, MsgType

log = logging.getLogger(__name__)

_WAKE = object()  # queued to a waiting fetch when another fetch frees a peer slot


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
        events: queue.SimpleQueue = queue.SimpleQueue()
        rr = 0  # round-robin position in rotation

        def candidates(h: str) -> list[str]:
            return [p for p in rotation if p not in dead and p not in struck[h]]

        def place(h: str) -> bool:
            """Ask a peer for h (or drop it if already stored). False if no candidate is free."""
            nonlocal rr
            if self.store.has(h):  # another fetch stored it meanwhile
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
                if self._request(by_id[pid], h, events):
                    inflight[h] = pid
                    return True
                dead.add(pid)  # connection already gone: try the next candidate

        with self._lock:
            self._waiters.add(events)
        try:
            while fresh or retry or inflight:
                # Rarest first: retried chunks by candidates left, ties in file order.
                for h in sorted(retry, key=lambda c: (len(candidates(c)), order[c])):
                    if place(h):
                        retry.discard(h)
                # Then fresh chunks in file order. They all share the same candidates (every
                # live peer), so once the head finds no free slot, none of the rest would.
                while fresh and place(fresh[0]):
                    fresh.popleft()
                if not (fresh or retry or inflight):
                    break
                # Something is inflight (its reply or failure will arrive), or every free slot
                # is held by another fetch(), whose release queues a _WAKE here.
                self._handle(events.get(), retry, inflight, struck, dead)
        finally:
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
        except ValueError as e:
            # CHUNK_CORRUPT (PROTOCOL.md §5.9, §10): discard, ask another peer.
            log.warning("CHUNK_CORRUPT: chunk %s from %s rejected: %s", h, pid, e)
            struck[h].add(pid)
            retry.add(h)

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
