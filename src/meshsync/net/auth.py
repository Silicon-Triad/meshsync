"""HMAC-SHA256 challenge-response handshake.

Owner: Muhammad Ahmed Qazi (CS-24045)
Contract (PROJECT_CONTEXT.md §8.3):
    Initiator and responder sides of HELLO/HELLO_ACK/AUTH/AUTH_OK (§6.5).
    mac_r = HMAC(K, 'R'|nonce_i|nonce_r), mac_i = HMAC(K, 'I'|nonce_r|nonce_i).
    Compare with hmac.compare_digest; HANDSHAKE_TIMEOUT; AUTH_FAILED on mismatch.
Status: stub, not implemented.
"""
