"""A minimal blocking WebSocket client (RFC 6455) on the standard library: the CLI's one request per connection.

Every CLI command pays Python's start-up, and importing asyncio and websockets cost about 130 ms of it (measured
3 Nov 2026). This client does what one request needs and no more: ws:// only (no TLS, no extensions, no
subprotocols), the opening handshake with its Sec-WebSocket-Accept check, masked frames out, unmasked text / binary
frames in (fragmented messages put together, a size cap), a ping answered with a pong, and the closing handshake.
It sends no keepalive pings of its own: a reply is simply waited for (transport.reply_wait), where websockets would
give up on a broker that stopped answering pings after about 40 s (CONNECTION_CLOSED instead of TIMEOUT).
transport.call_sync uses it; the broker, `transport.call` and the MCP server's persistent connection keep websockets.

Errors: BadURL, HandshakeFailed (no 101 Switching Protocols, or a reply that is not a valid upgrade), Closed (the
connection ended; `rcvd` is the peer's close frame — code and reason — when one came), TimeoutError (no answer in
time), OSError (could not connect).
"""

import binascii
import os
import socket
import sys
import time

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_HEADER_BYTES = 64 * 1024  # the HTTP reply to the opening handshake
CLOSE_TIMEOUT = 2.0           # s to wait for the broker's close frame once the reply is in
USER_AGENT = f"Python/{sys.version_info[0]}.{sys.version_info[1]} ppro_bridge.wsclient"
CLOSE_NAMES = {1000: "OK", 1001: "going away", 1002: "protocol error", 1003: "unsupported data",
               1005: "no status received [internal]", 1006: "connection closed abnormally [internal]",
               1007: "invalid frame payload data", 1008: "policy violation", 1009: "message too big",
               1011: "internal error"}


class WireError(Exception):
    """Base of what this client raises besides TimeoutError and OSError."""


class BadURL(WireError):
    pass


class HandshakeFailed(WireError):
    pass


class CloseFrame:
    def __init__(self, code: int, reason: str = ""):
        self.code, self.reason = code, reason

    def __str__(self):
        return f"{self.code} ({CLOSE_NAMES.get(self.code, 'unknown')}){' ' + self.reason if self.reason else ''}"


class Closed(WireError):
    """The connection ended. rcvd / sent: the close frames received / sent (None when there was none), as
    websockets.ConnectionClosed has them — transport reads rcvd.code 1008 as a refused pairing token."""

    def __init__(self, rcvd: CloseFrame | None = None, sent: CloseFrame | None = None):
        self.rcvd, self.sent = rcvd, sent
        if rcvd is None and sent is None:
            text = "no close frame received or sent"
        elif rcvd is None:
            text = f"sent {sent}; no close frame received"
        elif sent is None:
            text = f"received {rcvd}; no close frame sent"
        else:
            text = f"received {rcvd}; then sent {sent}"
        super().__init__(text)


def _split(url: str):
    """(host, port, resource, Host header) of a ws:// URL this client handles, else None."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        port = u.port or 80
    except ValueError:
        return None
    if u.scheme != "ws" or not u.hostname or u.username is not None or u.password is not None or u.fragment:
        return None
    host = u.hostname
    shown = f"[{host}]" if ":" in host else host
    return host, port, (u.path or "/") + (f"?{u.query}" if u.query else ""), (
        shown if port == 80 else f"{shown}:{port}")


def handles(url) -> bool:
    """True for a well-formed ws:// URL without user info; anything else (wss://, a malformed URL) is left to
    websockets, so its error stays the same."""
    return isinstance(url, str) and _split(url) is not None


def _mask(data: bytes, key: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    pad = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(n, "big")


def frame(opcode: int, payload: bytes = b"", fin: bool = True) -> bytes:
    """One masked client frame."""
    n = len(payload)
    head = bytearray([(0x80 if fin else 0) | opcode])
    if n < 126:
        head.append(0x80 | n)
    elif n < 1 << 16:
        head.append(0x80 | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(0x80 | 127)
        head += n.to_bytes(8, "big")
    key = os.urandom(4)
    return bytes(head) + key + _mask(payload, key)


def accept_key(key: str) -> str:
    try:
        from _sha1 import sha1  # the builtin one: hashlib would load OpenSSL (several ms) for one digest
    except ImportError:  # pragma: no cover
        from hashlib import sha1
    return binascii.b2a_base64(sha1(key.encode("ascii") + GUID).digest(), newline=False).decode("ascii")


class Connection:
    """An open connection: send_texts(), recv(timeout), close(). Use connect()."""

    def __init__(self, sock: socket.socket, max_size: int, buffered: bytes = b""):
        self.sock, self.max_size = sock, max_size
        self.buf = bytearray(buffered)
        self.closed = False
        self.sent_close: CloseFrame | None = None

    # ---- raw I/O (after the handshake every failure but a timeout is a closed connection)

    def _send(self, data: bytes):
        try:
            self.sock.sendall(data)
        except TimeoutError:
            raise
        except OSError:
            self._drop()
            raise Closed(None, self.sent_close) from None

    def _read(self, n: int, deadline: float | None) -> bytes:
        while len(self.buf) < n:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("timed out waiting for the broker")
                self.sock.settimeout(left)
            else:
                self.sock.settimeout(None)
            try:
                chunk = self.sock.recv(max(65536, n - len(self.buf)))
            except TimeoutError:
                raise
            except OSError:
                chunk = b""
            if not chunk:
                self._drop()
                raise Closed(None, self.sent_close)
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _drop(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _fail(self, code: int, reason: str):
        """A protocol problem: say so in a close frame (best effort) and drop the connection."""
        sent = CloseFrame(code, reason)
        if not self.closed:
            try:
                self.sock.sendall(frame(OP_CLOSE, code.to_bytes(2, "big") + reason.encode("utf-8")))
                self.sent_close = sent
            except OSError:
                pass
        self._drop()
        raise Closed(None, self.sent_close)

    def _read_frame(self, deadline: float | None):
        b0, b1 = self._read(2, deadline)
        fin, opcode, n = bool(b0 & 0x80), b0 & 0x0F, b1 & 0x7F
        if b0 & 0x70:
            self._fail(1002, "reserved bits must be 0")
        if b1 & 0x80:
            self._fail(1002, "incorrect masking")
        if n == 126:
            n = int.from_bytes(self._read(2, deadline), "big")
        elif n == 127:
            n = int.from_bytes(self._read(8, deadline), "big")
        if opcode >= OP_CLOSE and (not fin or n > 125):
            self._fail(1002, "bad control frame")
        if n > self.max_size:
            self._fail(1009, f"over size limit ({n} > {self.max_size} bytes)")
        return fin, opcode, self._read(n, deadline)

    def _closing(self, payload: bytes):
        """The peer's close frame: answer it (the closing handshake), drop the connection, raise Closed."""
        if len(payload) >= 2:
            rcvd = CloseFrame(int.from_bytes(payload[:2], "big"), payload[2:].decode("utf-8", "replace"))
        else:
            rcvd = CloseFrame(1005)
        if self.sent_close is None:
            try:
                self.sock.sendall(frame(OP_CLOSE, payload[:2]))
                self.sent_close = CloseFrame(rcvd.code) if len(payload) >= 2 else CloseFrame(1005)
            except OSError:
                pass
        self._drop()
        raise Closed(rcvd, self.sent_close)

    # ---- messages

    def send_texts(self, *texts: str):
        """Text messages, written in one go (no delay between them)."""
        if self.closed:
            raise Closed(None, self.sent_close)
        self._send(b"".join(frame(OP_TEXT, t.encode("utf-8")) for t in texts))

    def recv(self, timeout: float | None = None):
        """The next message (str for text, bytes for binary). Pings are answered on the way; TimeoutError when
        nothing complete came within `timeout` seconds."""
        if self.closed:
            raise Closed(None, self.sent_close)
        deadline = None if timeout is None else time.monotonic() + timeout
        kind, parts, size = None, [], 0
        while True:
            fin, opcode, payload = self._read_frame(deadline)
            if opcode == OP_PING:
                self._send(frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closing(payload)
            if opcode in (OP_TEXT, OP_BINARY):
                if kind is not None:
                    self._fail(1002, "expected a continuation frame")
                kind = opcode
            elif opcode != OP_CONT or kind is None:
                self._fail(1002, f"unexpected opcode {opcode}")
            size += len(payload)
            if size > self.max_size:
                self._fail(1009, f"over size limit ({size} > {self.max_size} bytes)")
            parts.append(payload)
            if fin:
                data = b"".join(parts)
                if kind == OP_BINARY:
                    return data
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    self._fail(1007, "invalid UTF-8")

    def close(self, timeout: float = CLOSE_TIMEOUT):
        """The closing handshake: a 1000 close frame, then wait (briefly) for the peer's; never raises."""
        if self.closed:
            return
        try:
            self.sock.sendall(frame(OP_CLOSE, (1000).to_bytes(2, "big")))
            self.sent_close = CloseFrame(1000)
            deadline = time.monotonic() + timeout
            while True:
                _, opcode, _ = self._read_frame(deadline)
                if opcode == OP_CLOSE:
                    break
        except (WireError, OSError):
            pass
        self._drop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(url: str, *, open_timeout: float = 5.0, max_size: int = 16 * 1024 * 1024) -> Connection:
    """Open a connection: TCP plus the opening handshake, both within open_timeout seconds."""
    where = _split(url)
    if where is None:
        raise BadURL(f"{url!r} is not a ws:// URL this client handles")
    host, port, resource, host_header = where
    deadline = time.monotonic() + open_timeout
    # an ASCII host goes to getaddrinfo as bytes: a str one is IDNA-encoded first, importing encodings.idna (~5 ms)
    sock = socket.create_connection((host.encode("ascii") if host.isascii() else host, port), timeout=open_timeout)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = binascii.b2a_base64(os.urandom(16), newline=False).decode("ascii")
        sock.sendall((f"GET {resource} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"User-Agent: {USER_AGENT}\r\n\r\n").encode("ascii"))
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            if len(raw) > MAX_HEADER_BYTES:
                raise HandshakeFailed("did not receive a valid HTTP response (headers too long)")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out during opening handshake")
            sock.settimeout(left)
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                raise
            except OSError:
                chunk = b""
            if not chunk:
                raise HandshakeFailed("did not receive a valid HTTP response")
            raw += chunk
        head, _, rest = bytes(raw).partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
            raise HandshakeFailed("did not receive a valid HTTP response")
        if parts[1] != "101":
            raise HandshakeFailed(f"server rejected WebSocket connection: HTTP {parts[1]}")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if not sep:
                raise HandshakeFailed("did not receive a valid HTTP response")
            name = name.strip().lower()
            headers[name] = f"{headers[name]}, {value.strip()}" if name in headers else value.strip()
        tokens = lambda h: {t.strip().lower() for t in headers.get(h, "").split(",")}  # noqa: E731
        if "websocket" not in tokens("upgrade"):
            raise HandshakeFailed(f"invalid Upgrade header: {headers.get('upgrade')!r}")
        if "upgrade" not in tokens("connection"):
            raise HandshakeFailed(f"invalid Connection header: {headers.get('connection')!r}")
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise HandshakeFailed(f"invalid Sec-WebSocket-Accept header: {headers.get('sec-websocket-accept')!r}")
        for name in ("sec-websocket-extensions", "sec-websocket-protocol"):
            if headers.get(name):
                raise HandshakeFailed(f"unexpected {name} header: {headers[name]!r} (none was offered)")
    except BaseException:
        sock.close()
        raise
    return Connection(sock, max_size, rest)
