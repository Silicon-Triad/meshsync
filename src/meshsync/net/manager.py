# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""PeerManager: dialing, accept loop and liveness.

Owner: Muhammad Ahmed Qazi (CS-24045)
Contract (PROJECT_CONTEXT.md §8.3):
    PeerManager.peers() -> list[PeerInfo]
    Events: peer_up(peer), peer_down(peer)
    Dial rule: lower peer_id initiates. PING/PONG liveness (PING_INTERVAL, MAX_MISSED_PONGS).
Status: stub, not implemented.
"""
