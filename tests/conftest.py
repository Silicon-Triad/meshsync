"""Shared fixtures: throwaway TLS certificates made by scripts/gen_certs.sh."""

import shutil
import subprocess
from pathlib import Path

import pytest

GEN_CERTS = Path(__file__).resolve().parents[1] / "scripts" / "gen_certs.sh"


def _gen(outdir: Path, *names: str) -> Path:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not on PATH; needed by scripts/gen_certs.sh")
    subprocess.run(["bash", str(GEN_CERTS), str(outdir), *names], check=True, capture_output=True)
    return outdir


@pytest.fixture(scope="session")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Group CA + alice + bob in a throwaway dir (never ~/.meshsync or real team certs)."""
    return _gen(tmp_path_factory.mktemp("certs"), "alice", "bob")


@pytest.fixture(scope="session")
def rogue(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A different CA with member 'mallory'; bob must not trust it."""
    return _gen(tmp_path_factory.mktemp("rogue"), "mallory")
