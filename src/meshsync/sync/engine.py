# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Sync engine: compare entries, conflicts, tombstones.

Owner: Mujtaba Jawaid Rao (CS-24047)
Contract (PROJECT_CONTEXT.md §8.3):
    Engine.on_remote_entry(peer, entry) -> Action(fetch/skip/conflict/delete)
    Conflict rule §7.2 (higher peer_id keeps name); tombstones (deleted: true) §7.3.
Status: stub, not implemented.
"""
