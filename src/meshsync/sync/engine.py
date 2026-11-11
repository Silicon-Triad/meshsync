# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Sync engine: compare entries, conflicts, tombstones.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    Engine.on_remote_entry(peer, entry) -> Action(fetch/skip/conflict/delete)
    Conflict rule §7.2 (higher peer_id keeps name); tombstones (deleted: true) §7.3.
    Engine.apply(action), Engine.on_fetched(entry) (node.EngineLike).
    Engine.on_change: callback for entries this peer must send as NOTIFY (issue #14 point 3,
    approved by all three members). build_node sets it to Node._on_local_change.
    Engine.lock: shared with Scanner.lock; build_node sets both to one lock (#14).

Files are written only through the assembler (transfer/assembler.py, #16), which builds in
.meshsync/tmp/ and verifies file_hash before os.replace (§7.3).

Before overwriting or deleting a file, the engine checks that the file on disk still matches
the local index entry. If it does not, the user edited it since the last scan: the engine leaves
it alone, and the scanner's next poll turns the edit into a newer entry (no data is lost).
"""

import hashlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from meshsync.config import Config
from meshsync.node import Action, IndexLike, StoreLike, load_or_create_peer_id
from meshsync.sync import vclock

log = logging.getLogger(__name__)

AssembleFn = Callable[..., Path]  # assemble(root, entry, store, dest=None) -> Path (#16)

_READ_SIZE = 1 << 20  # read size for hashing local files; local only, not a protocol value


def conflict_name(path: str, peer_id: str) -> str:
    """PROTOCOL.md §9.4: tag goes before the last dot of the file name, unless that dot is the
    first character. report.txt -> report.conflict-<id>.txt; Makefile -> Makefile.conflict-<id>.
    """
    head, sep, name = path.rpartition("/")
    tag = f".conflict-{peer_id}"
    dot = name.rfind(".")
    name = name + tag if dot <= 0 else name[:dot] + tag + name[dot:]
    return head + sep + name


def resolve(local: dict[str, int], remote: dict[str, int]) -> tuple[bool, str, str]:
    """For two concurrent vectors: (remote_keeps_name, winner_id, loser_tag) (§9.4).

    Uses only the vectors, so every peer gets the same answer. For each version, list the
    peers where it has the higher counter. The version whose list holds the overall highest
    peer_id keeps the name; the other is tagged with the highest peer_id in its own list.
    """
    peers = local.keys() | remote.keys()
    local_ahead = [p for p in peers if local.get(p, 0) > remote.get(p, 0)]
    remote_ahead = [p for p in peers if remote.get(p, 0) > local.get(p, 0)]
    if not local_ahead or not remote_ahead:
        raise ValueError("vectors are not concurrent")
    winner = max(local_ahead + remote_ahead)
    if winner in remote_ahead:
        return True, winner, max(local_ahead)
    return False, winner, max(remote_ahead)


def _default_assemble() -> AssembleFn:
    from meshsync.transfer import assembler

    fn = getattr(assembler, "assemble", None)
    if fn is None:
        raise NotImplementedError("transfer/assembler.py has no assemble() yet (#16)")
    return fn


class Engine:
    def __init__(
        self,
        config: Config,
        index: IndexLike,
        store: StoreLike,
        on_change: Callable[[dict], None] | None = None,
        *,
        peer_id: str | None = None,
        assemble: AssembleFn | None = None,
    ) -> None:
        self.root = Path(config.sync_dir)
        self.index = index
        self.store = store
        self.on_change = on_change
        # Same file Node and Scanner use (decision (l)).
        self.peer_id = peer_id if peer_id is not None else load_or_create_peer_id(self.root)
        self._assemble = assemble
        # Shared with the scanner: build_node sets both to one lock (#14). Held across the
        # version re-check, the disk check, assemble's os.replace and index.put, so the scanner
        # never sees a file and an index entry that disagree. on_change runs outside (R-4.7).
        self.lock: threading.RLock = threading.RLock()

    # ------------------------------------------------------------ node.EngineLike

    def on_remote_entry(self, peer: str, entry: dict) -> Action:
        """Decide what to do with a remote entry (PROTOCOL.md §9.3)."""
        with self.lock:
            local = self.index.get(entry["path"])
            order = vclock.compare(entry["version"], local["version"] if local else {})
            if order == "concurrent" and local is not None and _same_content(local, entry):
                # §9.4: same content is not a conflict; just merge the vectors.
                merged = dict(local, version=vclock.merge(local["version"], entry["version"]))
                self.index.put(merged)
                log.info("%s: concurrent but same content, merged vectors", entry["path"])
                return Action("skip", entry)
        if order == "dominates":
            return Action("delete" if entry["deleted"] else "fetch", entry)
        if order == "concurrent":
            log.info("%s: concurrent with %s's entry, conflict", entry["path"], peer)
            return Action("conflict", entry)
        return Action("skip", entry)  # local newer or equal (§9.3)

    def on_fetched(self, entry: dict) -> None:
        """Chunks are in the store: write the file and record the entry (§7.3)."""
        with self.lock:
            if entry["deleted"]:
                self._apply_delete(entry)
                return
            local = self._still(entry, "dominates")
            if local is False or not self._disk_matches(entry["path"], local):
                return
            if self._write(entry):
                self.index.put(entry)

    def apply(self, action: Action) -> None:
        if action.kind == "fetch":
            self.on_fetched(action.entry)
            return
        resolved = None
        with self.lock:
            if action.kind == "delete":
                self._apply_delete(action.entry)
            elif action.kind == "conflict":
                resolved = self._apply_conflict(action.entry)
        if resolved is not None and self.on_change is not None:
            self.on_change(resolved)  # outside the lock (R-4.7)

    # ------------------------------------------------------------ tombstones (§9.5)

    def _apply_delete(self, entry: dict) -> None:
        local = self._still(entry, "dominates")
        if local is False:
            return
        if self._disk_matches(entry["path"], local, quiet=True):
            if not self._unlink(entry["path"]):
                return
            log.info("deleted %s (remote tombstone)", entry["path"])
        else:
            # Edited since the last scan: keep it; the scanner sends it as a newer edit.
            log.info("%s changed locally; keeping it despite the remote tombstone", entry["path"])
        self.index.put(entry)

    # ------------------------------------------------------------ conflicts (§9.4)

    def _apply_conflict(self, remote: dict) -> dict | None:
        """Write both versions and record the merged entry; the entry to NOTIFY, or None.

        One side may be a tombstone (deleted on one peer, edited on another):
        - the tombstone keeps the name: nothing is written at the original path (the file is
          removed) and the edited version goes to the conflict name, so nothing is lost;
        - the edit keeps the name: a deletion has no content, so there is no copy to make; the
          edit is written at (or stays at) the original path.
        Remote chunks are already in the store: node.py fetches before apply (#50).
        The losing copy is written before the keeper. If the keeper write then fails, the copy
        stays on disk and the index is unchanged; the retry rewrites it, and if the scanner
        indexes it first, the same-hash rule merges the entries.
        """
        local = self._still(remote, "concurrent")
        if local is False or local is None:
            return None
        if not self._disk_matches(remote["path"], local):
            return None  # unscanned local edit: the scanner sends it, the next exchange resolves
        remote_wins, winner, tag = resolve(local["version"], remote["version"])
        keeper, loser = (remote, local) if remote_wins else (local, remote)
        copy_path = conflict_name(remote["path"], tag)
        if not loser["deleted"] and not self._write(loser, dest=copy_path):
            return None
        if remote_wins:
            if keeper["deleted"]:
                if not self._unlink(remote["path"]):
                    return None
            elif not self._write(keeper):
                return None
        version = vclock.merge(local["version"], remote["version"])
        resolver = winner == self.peer_id
        if resolver:  # issue #14 point 4: only the winning peer bumps and sends NOTIFY
            version = vclock.bump(version, self.peer_id)
        resolved = dict(keeper, version=version)
        self.index.put(resolved)
        if loser["deleted"]:
            log.info("%s: conflict with a deletion, edit kept", remote["path"])
        else:
            log.info("%s: conflict resolved, other version saved as %s", remote["path"], copy_path)
        return resolved if resolver else None

    # ------------------------------------------------------------ helpers (lock held)

    def _still(self, remote: dict, expected: str) -> "dict | None | bool":
        """Local entry if remote still compares as expected, else False (newer entry arrived)."""
        local = self.index.get(remote["path"])
        order = vclock.compare(remote["version"], local["version"] if local else {})
        if order != expected:
            log.debug("%s: index changed (%s), dropping remote entry", remote["path"], order)
            return False
        return local

    def _disk_matches(self, rel: str, local: dict | None, *, quiet: bool = False) -> bool:
        """True if the file on disk is what the local index entry says (or absent if none)."""
        want = None if local is None or local["deleted"] else local["file_hash"]
        if _hash_file(self.root / rel) != want:
            if not quiet:
                log.info("%s changed on disk since the last scan; waiting for the scanner", rel)
            return False
        return True

    def _write(self, entry: dict, dest: str | None = None) -> bool:
        if self._assemble is None:
            self._assemble = _default_assemble()
        try:
            self._assemble(self.root, entry, self.store, dest=dest)
        except (KeyError, ValueError, OSError) as e:
            # Missing chunk or bad content: leave the index unchanged so it is retried.
            log.warning("could not write %s: %r", dest or entry["path"], e)
            return False
        return True

    def _unlink(self, rel: str) -> bool:
        try:
            (self.root / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not delete %s: %s", rel, e)
            return False
        return True


def _same_content(a: dict, b: dict) -> bool:
    return (a["file_hash"], a["deleted"]) == (b["file_hash"], b["deleted"])


def _hash_file(path: Path) -> str | None:
    """SHA-256 of a regular file, or None if there is none."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while data := f.read(_READ_SIZE):
                h.update(data)
    except (FileNotFoundError, IsADirectoryError):
        return None
    return h.hexdigest()
