# FROZEN INTERFACE (AGENTS.md §6): changing or adding to the public interface in this file
# needs an issue and approval from all three members first (R-6.2).
"""Connection: one authenticated TCP link to one peer (PROJECT_CONTEXT.md §6.4, §6.7, §8.3).

Owner: Muhammad Ahmed Qazi (CS-24045). Issue #2.

    conn = establish(sock, key, me, initiator=True, tls_ctx=None)   # TLS + HMAC handshake
    conn.on(MsgType.INDEX_REQ, handler)                             # handler(conn, req_id, body)
    conn.start()                                                    # one reader thread
    reply = conn.request(MsgType.INDEX_REQ, {}).result()            # (MsgType, body)

States: AUTHENTICATED -> SYNCED -> CLOSED (§6.7). CONNECTED is covered by establish().

REQ_ID parity (PROTOCOL.md decision (h)): the initiator allocates odd ids, the responder even
ones. An incoming id with OUR parity is a reply to one of our requests; the other parity is the
peer's own request. Without this, the peer's request 5 could be mistaken for the reply to our 5.

Known limitation (follow-up issue): in TLS mode the reader thread (SSL_read) and senders
(SSL_write) use the same SSLSocket at the same time. _send_lock only serialises the writers.
OpenSSL does not promise that this is safe, and TLS 1.3 post-handshake messages make it more
likely to matter. It is untested under load; plain mode is not affected.

Idle connections: the reader gives up after READ_TIMEOUT seconds of silence. Until PeerManager
(#3) sends PINGs, a quiet link is closed after that time.

Threads (§9.1): one reader thread; all socket writes under _send_lock; _lock guards the state, id
counter and pending map. No socket I/O under _lock and no user callback or Future resolution
under any lock.

!!! HANDLERS RUN ON THE READER THREAD. A handler must be quick, and it must NEVER wait on a
Future from this same connection (the reply could only be read by the thread that is waiting:
deadlock). Long work (disk, hashing, fetching) must be handed to another thread by the caller.
"""

import logging
import socket
import ssl
import threading
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError
from enum import Enum
from typing import Any

from meshsync.net import auth, tls
from meshsync.net.auth import PeerIdentity
from meshsync.protocol.constants import (
    FATAL_ERRORS,
    MAX_MISSED_PONGS,
    MAX_REQ_ID,
    PING_INTERVAL,
    ErrorCode,
    MsgType,
)
from meshsync.protocol.framing import ProtocolError, recv_msg, send_msg
from meshsync.protocol.messages import decode_body, encode_body

log = logging.getLogger(__name__)

# A dead peer must not hang the reader forever. Real liveness (PING/PONG) is PeerManager's (#3).
READ_TIMEOUT = PING_INTERVAL * (MAX_MISSED_PONGS + 1)

_HANDSHAKE_TYPES = {MsgType.HELLO, MsgType.HELLO_ACK, MsgType.AUTH, MsgType.AUTH_OK}

Handler = Callable[["Connection", int, Any], None]
CloseCallback = Callable[["Connection", str], None]


class State(Enum):
    AUTHENTICATED = "AUTHENTICATED"
    SYNCED = "SYNCED"
    CLOSED = "CLOSED"


class RemoteError(Exception):
    """The peer answered one of our requests with ERROR."""

    def __init__(self, code: ErrorCode, msg: str) -> None:
        super().__init__(f"{code.name}: {msg}")
        self.code = code
        self.msg = msg


def _settle(fut: Future, result: Any = None, exc: BaseException | None = None) -> None:
    """Resolve a Future; ignore one the caller already cancelled."""
    try:
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)
    except InvalidStateError:
        log.debug("future already done")


# ---------------------------------------------------------------- socket -> Connection


def establish(
    sock: socket.socket,
    key: bytes,
    me: PeerIdentity,
    *,
    initiator: bool,
    tls_ctx: ssl.SSLContext | None,
) -> "Connection":
    """TLS (optional) + HMAC handshake on a fresh socket; returns an AUTHENTICATED Connection.

    The caller still has to call .start(). On any failure the socket is closed and the error
    re-raised.
    """
    try:
        if tls_ctx is not None:  # §6.6: wrap before HELLO
            sock = tls.wrap_client(sock, tls_ctx) if initiator else tls.wrap_server(sock, tls_ctx)
        handshake = auth.initiator_handshake if initiator else auth.responder_handshake
        remote = handshake(sock, key, me)  # §6.5
        if tls_ctx is not None:
            _check_cert_name(sock, remote)
        return Connection(sock, remote, initiator)
    except Exception:
        sock.close()
        raise


def _check_cert_name(sock: socket.socket, remote: PeerIdentity) -> None:
    """Decision (e): the verified certificate's CN must equal the HELLO name."""
    try:
        cert_name: str | None = tls.peer_cert_name(sock)  # type: ignore[arg-type]
    except ValueError as e:
        log.warning("no usable peer certificate: %s", e)
        cert_name = None
    if cert_name == remote.name:
        return
    err = ProtocolError(
        ErrorCode.AUTH_FAILED, f"certificate name {cert_name!r} != HELLO name {remote.name!r}"
    )
    log.warning("%s", err)
    try:
        body = encode_body(MsgType.ERROR, {"code": int(err.code), "msg": str(err)})
        send_msg(sock, MsgType.ERROR, auth.HANDSHAKE_REQ_ID, body)
    except OSError as e:
        log.debug("could not send ERROR: %s", e)
    raise err


# ---------------------------------------------------------------- Connection


class Connection:
    def __init__(self, sock: socket.socket, remote: PeerIdentity, initiator: bool) -> None:
        self._sock = sock
        self._remote = remote
        self._parity = 1 if initiator else 0  # odd ids for the initiator, even for the responder
        self._next_id = 1 if initiator else 2
        self._first_id = self._next_id
        self._send_lock = threading.Lock()  # serialises socket writes
        self._lock = threading.Lock()  # guards everything below
        self._state = State.AUTHENTICATED
        self._pending: dict[int, Future] = {}
        self._handlers: dict[MsgType, Handler] = {}
        self._close_callbacks: list[CloseCallback] = []
        self._thread = threading.Thread(
            target=self._read_loop, name=f"conn-{remote.name}", daemon=True
        )
        sock.settimeout(READ_TIMEOUT)

    # ------------------------------------------------------------ properties

    @property
    def remote(self) -> PeerIdentity:
        return self._remote

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def closed(self) -> bool:
        return self.state is State.CLOSED

    # ------------------------------------------------------------ registration

    def start(self) -> None:
        self._thread.start()

    def on(self, msg_type: MsgType, fn: Handler) -> None:
        """Handle incoming requests/unsolicited messages of this type: fn(conn, req_id, body).

        One handler per type; registering again replaces it. Runs on the reader thread (see the
        module docstring).
        """
        with self._lock:
            self._handlers[msg_type] = fn

    def on_close(self, fn: CloseCallback) -> None:
        """fn(conn, reason) runs once when the connection closes (at once if already closed)."""
        with self._lock:
            already_closed = self._state is State.CLOSED
            if not already_closed:
                self._close_callbacks.append(fn)
        if already_closed:
            self._run_callback(fn, "already closed")

    def mark_synced(self) -> None:
        """The sync layer calls this once the INDEX exchange is done (§6.7)."""
        with self._lock:
            if self._state is not State.AUTHENTICATED:
                return
            self._state = State.SYNCED
        log.info("connection to %s is SYNCED", self._remote.name)

    # ------------------------------------------------------------ sending

    def request(self, msg_type: MsgType, body: dict | bytes) -> Future:
        """Send a request. Future: (MsgType, body), or RemoteError / ConnectionError.

        The reply's type is not checked against the request's: that is the caller's job.
        """
        data = encode_body(msg_type, body)
        fut: Future = Future()
        with self._lock:
            if self._state is State.CLOSED:
                raise ConnectionError("connection closed")
            req_id = self._alloc_id()
            self._pending[req_id] = fut
        self._write(msg_type, req_id, data)  # on failure this closes, which fails fut too
        return fut

    def send(self, msg_type: MsgType, body: dict | bytes, req_id: int = 0) -> None:
        """Unsolicited message (NOTIFY, BYE) or a reply (req_id = the request's id)."""
        if req_id and req_id % 2 == self._parity:  # our parity = ids of OUR requests
            raise ValueError(f"req_id {req_id} is one of our request ids; replies echo the peer's")
        data = encode_body(msg_type, body)
        if self.closed:
            raise ConnectionError("connection closed")
        self._write(msg_type, req_id, data)

    def reply(self, req_id: int, msg_type: MsgType, body: dict | bytes) -> None:
        """Answer the peer's request `req_id`; for handlers."""
        self.send(msg_type, body, req_id)

    def _alloc_id(self) -> int:
        """Next free id of our parity in 1..MAX_REQ_ID, wrapping, never 0. Caller holds _lock."""
        if len(self._pending) >= MAX_REQ_ID // 2:  # every id of our parity is in use
            raise RuntimeError("no free REQ_ID")
        while True:
            req_id = self._next_id
            self._next_id = req_id + 2 if req_id + 2 <= MAX_REQ_ID else self._first_id
            if req_id not in self._pending:
                return req_id

    def _send_raw(self, msg_type: MsgType, req_id: int, data: bytes) -> None:
        with self._send_lock:  # the only place that writes to the socket (§9.1)
            send_msg(self._sock, msg_type, req_id, data)

    def _write(self, msg_type: MsgType, req_id: int, data: bytes) -> None:
        try:
            self._send_raw(msg_type, req_id, data)
        except OSError as e:
            self.close(f"send failed: {e}", send_bye=False)
            raise ConnectionError(f"send failed: {e}") from e

    def _send_error(self, code: ErrorCode, msg: str, req_id: int = 0) -> None:
        """Best effort: tell the peer why (§6.8). Never raises."""
        try:
            self._send_raw(
                MsgType.ERROR, req_id, encode_body(MsgType.ERROR, {"code": code, "msg": msg})
            )
        except OSError as e:
            log.debug("could not send ERROR: %s", e)

    # ------------------------------------------------------------ closing

    def close(self, reason: str = "", send_bye: bool = True) -> None:
        """Close the link. Safe to call more than once and from any thread."""
        with self._lock:
            if self._state is State.CLOSED:
                return
            self._state = State.CLOSED
            pending, self._pending = self._pending, {}
            callbacks, self._close_callbacks = self._close_callbacks, []
        log.info("closing connection to %s: %s", self._remote.name, reason or "no reason")
        if send_bye:
            self._try_send_bye(reason)
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wakes the reader thread out of recv
        except OSError as e:
            log.debug("shutdown: %s", e)
        self._sock.close()
        for fut in pending.values():
            _settle(fut, exc=ConnectionError(reason or "connection closed"))
        for fn in callbacks:
            self._run_callback(fn, reason)

    def _try_send_bye(self, reason: str) -> None:
        """Best-effort BYE. If a sender holds the lock (maybe stuck on a peer that does not read),
        skip it: waiting could block close() for the whole socket timeout. The shutdown that
        follows also unblocks that sender."""
        if not self._send_lock.acquire(blocking=False):
            log.debug("sender busy, skipping BYE")
            return
        try:
            send_msg(self._sock, MsgType.BYE, 0, encode_body(MsgType.BYE, {"reason": reason}))
        except OSError as e:
            log.debug("could not send BYE: %s", e)
        finally:
            self._send_lock.release()

    def _run_callback(self, fn: CloseCallback, reason: str) -> None:
        try:
            fn(self, reason)
        except Exception:
            log.exception("on_close callback failed")

    # ------------------------------------------------------------ reader thread

    def _read_loop(self) -> None:
        try:
            while not self.closed:
                msg_type, req_id, data = recv_msg(self._sock)
                self._dispatch(msg_type, req_id, decode_body(msg_type, data))
        except ProtocolError as e:
            log.warning("protocol error from %s: %s", self._remote.name, e)
            self._send_error(e.code, str(e))
            self.close(str(e), send_bye=False)
        except OSError as e:  # EOF (ConnectionError), socket timeout, reset, closed by close()
            self.close(f"{type(e).__name__}: {e}", send_bye=False)
        except Exception:
            log.exception("reader thread crashed")
            self.close("internal error", send_bye=False)

    def _dispatch(self, msg_type: MsgType, req_id: int, body: Any) -> None:
        ours = req_id != 0 and req_id % 2 == self._parity  # a reply to one of our requests

        if msg_type == MsgType.ERROR:
            self._on_error(req_id, ours, body)
        elif msg_type in _HANDSHAKE_TYPES:  # handshake is over (§6.7)
            self._send_error(
                ErrorCode.ILLEGAL_STATE, f"{msg_type.name} after authentication", req_id
            )
            self.close(f"illegal {msg_type.name}", send_bye=False)
        elif ours:
            with self._lock:
                fut = self._pending.pop(req_id, None)
            if fut is None:
                log.debug("reply %d has no pending request (cancelled or closed)", req_id)
            else:
                _settle(fut, result=(msg_type, body))
        elif msg_type == MsgType.PING:  # liveness timing belongs to PeerManager (#3)
            self.reply(req_id, MsgType.PONG, {"t": body["t"]})
        elif msg_type == MsgType.BYE:
            self.close(f"peer sent BYE: {body['reason']}", send_bye=False)
        else:
            self._call_handler(msg_type, req_id, body)

    def _on_error(self, req_id: int, ours: bool, body: dict) -> None:
        code = ErrorCode(body["code"])
        with self._lock:
            fut = self._pending.pop(req_id, None) if ours else None
        if fut is not None:
            _settle(fut, exc=RemoteError(code, body["msg"]))
        else:
            log.warning("ERROR from %s: %s: %s", self._remote.name, code.name, body["msg"])
        if code in FATAL_ERRORS:  # §6.8
            self.close(f"fatal ERROR {code.name}", send_bye=False)

    def _call_handler(self, msg_type: MsgType, req_id: int, body: Any) -> None:
        with self._lock:
            handler = self._handlers.get(msg_type)
        if handler is None:
            if msg_type == MsgType.PONG:  # nobody is timing pings yet
                return
            log.warning("no handler for %s from %s", msg_type.name, self._remote.name)
            self._send_error(ErrorCode.INTERNAL, "unsupported", req_id)
            return
        try:
            handler(self, req_id, body)
        except Exception:  # a buggy handler must not take the connection down
            log.exception("handler for %s failed", msg_type.name)
            self._send_error(ErrorCode.INTERNAL, "handler failed", req_id)
