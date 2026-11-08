"""Tests for sync/vclock.py (D2, §7.2)."""

import pytest

from meshsync.sync.vclock import bump, compare, merge

A, B, C = "A1F3C9D2", "B77C0E11", "C0FFEE00"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ({}, {}, "equal"),
        ({A: 4, B: 2}, {A: 4, B: 2}, "equal"),
        ({A: 0}, {}, "equal"),  # missing peer counts as 0
        ({A: 5, B: 2}, {A: 4, B: 2}, "dominates"),
        ({A: 4, B: 2}, {A: 5, B: 2}, "dominated"),
        ({A: 1}, {}, "dominates"),
        ({}, {A: 1}, "dominated"),
        ({A: 4, B: 2, C: 1}, {A: 4, B: 2}, "dominates"),  # extra peer on one side
        ({A: 5, B: 2}, {A: 4, B: 3}, "concurrent"),
        ({A: 1}, {B: 1}, "concurrent"),  # disjoint peers
    ],
)
def test_compare(a: dict, b: dict, expected: str) -> None:
    assert compare(a, b) == expected


def test_compare_is_antisymmetric() -> None:
    a, b = {A: 5, B: 2}, {A: 4, B: 2}
    assert compare(a, b) == "dominates"
    assert compare(b, a) == "dominated"
    assert compare({A: 1}, {B: 1}) == compare({B: 1}, {A: 1}) == "concurrent"


def test_merge_takes_max_per_peer() -> None:
    assert merge({A: 5, B: 2}, {A: 4, B: 3, C: 1}) == {A: 5, B: 3, C: 1}
    assert merge({}, {}) == {}


def test_merge_result_dominates_or_equals_both() -> None:
    a, b = {A: 5, B: 2}, {A: 4, B: 3}
    m = merge(a, b)
    assert compare(m, a) == "dominates"
    assert compare(m, b) == "dominates"
    assert compare(merge(a, a), a) == "equal"


def test_merge_does_not_mutate() -> None:
    a, b = {A: 1}, {B: 2}
    merge(a, b)
    assert a == {A: 1} and b == {B: 2}


def test_bump_existing_and_new_peer() -> None:
    assert bump({A: 4, B: 2}, A) == {A: 5, B: 2}
    assert bump({}, B) == {B: 1}


def test_bump_does_not_mutate_and_dominates() -> None:
    v = {A: 4}
    out = bump(v, A)
    assert v == {A: 4}
    assert out is not v
    assert compare(out, v) == "dominates"


def test_conflict_resolution_vector_dominates_both() -> None:
    # §7.2 concurrent case: merged vector + own bump must beat both sides.
    local, remote = {A: 5, B: 2}, {A: 4, B: 3}
    resolved = bump(merge(local, remote), A)
    assert compare(resolved, local) == "dominates"
    assert compare(resolved, remote) == "dominates"
