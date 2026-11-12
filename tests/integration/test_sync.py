"""End-to-end sync between real meshsync peers on 127.0.0.1 (#62; §7, §10, §11, §14)."""

import os

import pytest
from mesh import Mesh, settle, sha256

from meshsync.protocol.constants import CDC_MAX, HEADER_SIZE

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ABC = ["a", "b", "c"]


def same_everywhere(mesh: Mesh, rel: str, digest: str) -> bool:
    for p in mesh.peers.values():
        f = p.path(rel)
        if not f.is_file() or sha256(f) != digest:
            return False
    return True


def check_create(mesh: Mesh) -> None:
    a = mesh["a"]
    (a.path("docs")).mkdir()
    a.path("docs/hello.bin").write_bytes(os.urandom(3 * CDC_MAX))  # several chunks (D10)
    digest = sha256(a.path("docs/hello.bin"))
    mesh.wait(lambda: same_everywhere(mesh, "docs/hello.bin", digest), "create on b and c")


def test_create_static_peers(make_mesh) -> None:
    mesh = make_mesh(ABC)
    mesh.start()
    check_create(mesh)


def test_create_udp_discovery(make_mesh) -> None:
    # §6.2: no --peer, peers find each other by ANNOUNCE on the shared UDP port.
    mesh = make_mesh(ABC, discovery=True)
    mesh.start()
    check_create(mesh)


def test_create_tls(make_mesh) -> None:
    # D9: TLS 1.3 with certs from scripts/gen_certs.sh.
    mesh = make_mesh(ABC, tls=True)
    mesh.start()
    check_create(mesh)


def test_small_edit_sends_only_changed_chunks(make_mesh) -> None:
    # §11 condition 1: a small edit to a big file moves only the changed chunks (D10).
    mesh = make_mesh(["a", "b"])
    a, b = mesh["a"], mesh["b"]
    size = 16 * CDC_MAX
    a.path("big.bin").write_bytes(os.urandom(size))
    mesh.start()
    digest = sha256(a.path("big.bin"))
    mesh.wait(lambda: same_everywhere(mesh, "big.bin", digest), "initial copy on b")
    first = b.recv_bytes("CHUNK_DATA")
    assert first >= size  # the whole file came over CHUNK_DATA

    mark = len(b.log_text())
    data = bytearray(a.path("big.bin").read_bytes())
    data[size // 2 : size // 2 + 8] = b"EDITEDED"  # in-place, 8 bytes
    a.path("big.bin").write_bytes(bytes(data))
    digest = sha256(a.path("big.bin"))
    mesh.wait(lambda: same_everywhere(mesh, "big.bin", digest), "edit on b")

    delta = b.recv_bytes("CHUNK_DATA", since=mark)
    # The edit touches one chunk, or two if it lands near a boundary; each is at most CDC_MAX.
    assert 0 < delta <= 2 * (CDC_MAX + HEADER_SIZE), delta
    assert delta < size // 4, (delta, size)


def test_delete_propagates(make_mesh) -> None:
    # §7.3: a delete becomes a tombstone and removes the file everywhere.
    mesh = make_mesh(ABC)
    mesh.start()
    a = mesh["a"]
    a.path("gone.txt").write_bytes(b"short-lived\n")
    digest = sha256(a.path("gone.txt"))
    mesh.wait(lambda: same_everywhere(mesh, "gone.txt", digest), "file on all peers")
    a.path("gone.txt").unlink()
    mesh.wait(
        lambda: not any(p.path("gone.txt").exists() for p in mesh.peers.values()),
        "delete on b and c",
    )


def test_offline_peer_catches_up(make_mesh) -> None:
    # §10 row 3: b misses changes while down and gets them from the index exchange on rejoin.
    mesh = make_mesh(ABC)
    mesh.start()
    a, b = mesh["a"], mesh["b"]
    a.path("old.txt").write_bytes(b"before\n")
    a.path("keep.txt").write_bytes(b"v1\n")
    mesh.wait(
        lambda: (
            same_everywhere(mesh, "old.txt", sha256(a.path("old.txt")))
            and same_everywhere(mesh, "keep.txt", sha256(a.path("keep.txt")))
        ),
        "initial files",
    )

    b.stop()
    a.path("new.bin").write_bytes(os.urandom(2 * CDC_MAX))
    a.path("keep.txt").write_bytes(b"v2, edited while b was down\n")
    a.path("old.txt").unlink()
    others = {"a": mesh["a"], "c": mesh["c"]}
    mesh.wait(
        lambda: all(p.path("new.bin").exists() and not p.path("old.txt").exists()
                    for p in others.values()),
        "changes on a and c",
    )  # fmt: skip

    b.start()
    want = {r: sha256(a.path(r)) for r in ("new.bin", "keep.txt")}
    mesh.wait(
        lambda: (
            all(same_everywhere(mesh, r, h) for r, h in want.items())
            and not b.path("old.txt").exists()
        ),
        "b catches up after rejoin",
    )


def test_concurrent_edit_makes_conflict_copy(make_mesh) -> None:
    # §7.2, §10 row 2: a and b edit the same file while apart; on reconnect both versions
    # survive on every peer, one of them under the conflict name.
    apart = make_mesh(["a", "b"], isolated=True)
    apart["a"].path("notes.txt").write_bytes(b"a's version\n")
    apart["b"].path("notes.txt").write_bytes(b"b's version\n")
    apart.start()
    settle()  # each indexes its own version: version vectors {a:1} and {b:1}, concurrent
    apart.stop()

    mesh = make_mesh(ABC)
    for name in ("a", "b"):  # reuse the edited folders (and their peer_id/index) in the mesh
        mesh[name].dir.rmdir()
        apart[name].dir.rename(mesh[name].dir)
    mesh.start()

    versions = {b"a's version\n", b"b's version\n"}

    def resolved() -> bool:
        for p in mesh.peers.values():
            got = {p.path(f).read_bytes() for f in p.files() if f.startswith("notes")}
            names = {f for f in p.files() if f.startswith("notes")}
            if got != versions or len(names) != 2 or "notes.txt" not in names:
                return False
        return len({frozenset(p.files()) for p in mesh.peers.values()}) == 1

    mesh.wait(resolved, "both versions on every peer")
    copy = (mesh["a"].files() - {"notes.txt"}).pop()
    assert copy.startswith("notes.conflict-") and copy.endswith(".txt"), copy
