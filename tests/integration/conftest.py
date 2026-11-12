"""Fixture for multi-peer integration tests (#62). Helpers live in mesh.py."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from mesh import Mesh


@pytest.fixture
def make_mesh(tmp_path: Path) -> Iterator[Callable[..., Mesh]]:
    meshes: list[Mesh] = []

    def make(names: list[str], **kw: bool) -> Mesh:
        mesh = Mesh(tmp_path / f"mesh{len(meshes)}", names, **kw)
        meshes.append(mesh)
        return mesh

    yield make
    for mesh in meshes:
        mesh.stop()
