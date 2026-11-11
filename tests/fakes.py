"""In-memory fakes for the node.py Protocols (IndexLike, StoreLike, EngineLike, ...).

Meant for everyone's tests, so they stay small. Entries are real §7.1 entries:
{path, size, file_hash, chunks: [sha256 hex], version: {peer_id: counter}, deleted}.
"""

import hashlib
from collections.abc import Callable

from meshsync.net.manager import PeerInfo
from meshsync.node import Action
from meshsync.protocol.constants import MsgType


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_entry(path: str, chunks: list[bytes], version: dict[str, int]) -> dict:
    """A live entry whose chunks are the given byte strings."""
    return {
        "path": path,
        "size": sum(len(c) for c in chunks),
        "file_hash": sha(b"".join(chunks)),
        "chunks": [sha(c) for c in chunks],
        "version": version,
        "deleted": False,
    }


class FakeIndex:
    """Dict index; `rev` goes up on every put."""

    def __init__(self) -> None:
        self._files: dict[str, dict] = {}
        self.rev = 0

    def get(self, path: str) -> dict | None:
        return self._files.get(path)

    def put(self, entry: dict) -> None:
        self._files[entry["path"]] = entry
        self.rev += 1

    def entries(self) -> list[dict]:
        return list(self._files.values())


class FakeStore:
    """Dict chunk store. get() of a missing hash raises KeyError."""

    def __init__(self) -> None:
        self._chunks: dict[str, bytes] = {}

    def has(self, h: str) -> bool:
        return h in self._chunks

    def get(self, h: str) -> bytes:
        return self._chunks[h]

    def put(self, h: str, data: bytes) -> None:
        self._chunks[h] = data


def dominates(a: dict, b: dict) -> bool:
    """True if version vector a is >= b everywhere and > b somewhere."""
    keys = set(a) | set(b)
    return all(a.get(k, 0) >= b.get(k, 0) for k in keys) and a != b


class FakeEngine:
    """Trivial rule: fetch if the remote version dominates ours (or we have none), else skip.

    Set `force` to "conflict", "skip" or "delete" to return that action for every entry.
    Records what Node did in `applied` and `fetched`.
    """

    def __init__(self, index: FakeIndex, force: str | None = None) -> None:
        self.index = index
        self.force = force
        self.applied: list[Action] = []
        self.fetched: list[dict] = []

    def on_remote_entry(self, peer_id: str, entry: dict) -> Action:
        if self.force:
            return Action(self.force, entry)  # type: ignore[arg-type]
        mine = self.index.get(entry["path"])
        if mine is None or dominates(entry["version"], mine["version"]):
            return Action("fetch", entry)
        return Action("skip", entry)

    def apply(self, action: Action) -> None:
        self.applied.append(action)

    def on_fetched(self, entry: dict) -> None:
        self.index.put(entry)
        self.fetched.append(entry)


class FakeScheduler:
    """Pulls every missing chunk with CHUNK_REQ from the first peer. `delay` slows it down."""

    def __init__(self, store: FakeStore, delay: float = 0.0, sleep: Callable | None = None) -> None:
        self.store = store
        self.delay = delay
        self._sleep = sleep
        self.calls: list[dict] = []  # every entry fetch() was called with
        self.fail_paths: set[str] = set()  # fetch() raises ConnectionError for these paths

    def fetch(self, entry: dict, peers: list[PeerInfo]) -> None:
        self.calls.append(entry)
        if entry["path"] in self.fail_paths:
            raise ConnectionError(f"fake fetch failure for {entry['path']}")
        if self.delay and self._sleep:
            self._sleep(self.delay)
        for h in entry["chunks"]:
            if self.store.has(h):
                continue
            _, data = peers[0].conn.request(MsgType.CHUNK_REQ, {"hash": h}).result(5)
            self.store.put(h, data)


class FakeScanner:
    """Does nothing until a test calls trigger(entry), which acts like a local file change."""

    def __init__(self) -> None:
        self.on_change: Callable[[dict], None] | None = None
        self.stopped = False

    def start(self, on_change: Callable[[dict], None]) -> None:
        self.on_change = on_change

    def stop(self) -> None:
        self.stopped = True

    def trigger(self, entry: dict) -> None:
        assert self.on_change is not None
        self.on_change(entry)
