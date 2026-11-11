"""Tests for metrics/logger.py (§8.3, §11, #17). In-memory handlers, no network."""

import csv
import io
import logging
import threading
from collections.abc import Iterator

import pytest

from meshsync.metrics import logger as metrics


@pytest.fixture
def csv_out() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger(metrics.CSV_LOGGER)
    lg.addHandler(h)
    yield buf
    lg.removeHandler(h)


def rows(buf: io.StringIO) -> list[list[str]]:
    return list(csv.reader(io.StringIO(buf.getvalue())))


def test_record_writes_one_csv_row(csv_out: io.StringIO) -> None:
    metrics.record("send", "bob", "PING", 20, 1700000000.25)
    assert rows(csv_out) == [["1700000000.250000", "send", "bob", "PING", "20"]]
    assert metrics.CSV_HEADER.split(",") == ["t", "direction", "peer", "type", "size"]


def test_peer_with_comma_is_quoted(csv_out: io.StringIO) -> None:
    metrics.record("recv", "a,b", "PONG", 20, 1.0)
    assert rows(csv_out)[0][2] == "a,b"


def test_human_line_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="meshsync.metrics.logger"):
        metrics.record("recv", "alice", "INDEX_REQ", 14, 2.5)
    assert "recv peer=alice type=INDEX_REQ size=14 t=2.500000" in caplog.text


def test_csv_logger_does_not_propagate_to_app_log() -> None:
    # CSV rows must not end up in the human log (pytest adds its own handlers, so check the flag)
    assert logging.getLogger(metrics.CSV_LOGGER).propagate is False


@pytest.mark.parametrize(("direction", "size"), [("out", 1), ("send", -1)])
def test_bad_arguments_raise(direction: str, size: int) -> None:
    with pytest.raises(ValueError):
        metrics.record(direction, "bob", "PING", size, 1.0)


def test_concurrent_records_do_not_interleave(csv_out: io.StringIO) -> None:
    def work(i: int) -> None:
        for _ in range(50):
            metrics.record("send", f"p{i}", "CHUNK_DATA", 1000, 1.0)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    got = rows(csv_out)
    assert len(got) == 400 and all(len(r) == 5 for r in got)
