"""Version vector operations.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    compare(a, b) -> dominates / dominated / equal / concurrent
    merge(a, b) -> vector; bump(v, peer_id) -> vector (D2).

A vector maps peer_id -> counter (§7.1). A peer missing from a vector counts as 0, so
{"A1F3C9D2": 0} equals {}. Functions never mutate their arguments.
"""

from typing import Literal

Ordering = Literal["dominates", "dominated", "equal", "concurrent"]


def compare(a: dict[str, int], b: dict[str, int]) -> Ordering:
    """Order a relative to b (§7.2, D2). "dominates" means a is strictly newer than b."""
    a_ahead = b_ahead = False
    for peer in a.keys() | b.keys():
        x, y = a.get(peer, 0), b.get(peer, 0)
        if x > y:
            a_ahead = True
        elif y > x:
            b_ahead = True
    if a_ahead and b_ahead:
        return "concurrent"
    if a_ahead:
        return "dominates"
    if b_ahead:
        return "dominated"
    return "equal"


def merge(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Element-wise max of a and b (§7.2: conflict resolution uses the merged vector)."""
    return {peer: max(a.get(peer, 0), b.get(peer, 0)) for peer in a.keys() | b.keys()}


def bump(v: dict[str, int], peer_id: str) -> dict[str, int]:
    """Copy of v with peer_id's counter incremented by one (§7.3: local edit bumps own)."""
    out = dict(v)
    out[peer_id] = out.get(peer_id, 0) + 1
    return out
