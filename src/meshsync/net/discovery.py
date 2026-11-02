"""UDP discovery: ANNOUNCE/LEAVE broadcast and peer table.

Owner: Muhammad Ahmed Qazi (CS-24045)
Contract (PROJECT_CONTEXT.md §8.3):
    Send ANNOUNCE every ANNOUNCE_INTERVAL, LEAVE on shutdown (§6.2).
    tag = HMAC-SHA256(K, canonical_json_without_tag) (D11); drop on bad tag or skew > 30 s.
    Decision (b): also drop ts <= last accepted ts for that peer_id (replay).
    Peer table with PEER_EXPIRY expiry.
Status: stub, not implemented.
"""
