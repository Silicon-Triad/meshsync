"""Message bodies: builders and validators per MsgType.

Owner: Muhammad Ahmed Qazi (CS-24045)
Contract (PROJECT_CONTEXT.md §8.3):
    build_<type>(...) -> bytes / parse_<type>(body: bytes) -> dict for each MsgType (JSON <-> dict).
    Path validation: POSIX, relative, never containing '..' (reject on receipt, §7.1).
    Consumes: framing (raw bytes), constants.MsgType.
Status: stub, not implemented.
"""
