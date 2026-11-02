# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Connection: one authenticated TCP link to one peer.

Owner: Muhammad Ahmed Qazi (CS-24045)
Contract (PROJECT_CONTEXT.md §8.3):
    Connection.request(type, body) -> Future   (reply matched by REQ_ID)
    Connection.on(type, fn)                    (handler registry)
    Connection.send(...)                       (all sends under a lock)
    One reader thread per connection; REQ_ID -> Future map; state machine §6.7.
Status: stub, not implemented.
"""
