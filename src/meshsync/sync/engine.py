"""Sync engine: compare entries, conflicts, tombstones.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    Engine.on_remote_entry(peer, entry) -> Action(fetch/skip/conflict/delete)
    Conflict rule §7.2 (higher peer_id keeps name); tombstones (deleted: true) §7.3.
Status: stub, not implemented.
"""
