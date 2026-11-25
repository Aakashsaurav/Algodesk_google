"""
core/ws_protocol.py
===================
Minimal, pure-stdlib RFC 6455 WebSocket helper so `web/local_server.py`
supports native `/ws/live` WebSocket connections without external packages.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
from typing import Optional, Tuple

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


def compute_accept_key(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key.strip() + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def perform_handshake(handler: AnySocketHandler) -> bool:
    """Validate incoming HTTP Upgrade headers and emit HTTP 101 Switching Protocols."""
    key = handler.headers.get("Sec-WebSocket-Key")
    upgrade = (handler.headers.get("Upgrade") or "").strip().lower()
    if not key or upgrade != "websocket":
        return False

    accept = compute_accept_key(key)
    proto_hdr = handler.headers.get("Sec-WebSocket-Protocol") or ""
    requested_protos = [p.strip() for p in proto_hdr.split(",") if p.strip()]
    proto_line = "Sec-WebSocket-Protocol: algodesk.v1\r\n" if "algodesk.v1" in requested_protos else ""
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        f"{proto_line}"
        "Cache-Control: no-store\r\n"
        "\r\n"
    ).encode("ascii")
    handler.wfile.write(response)
    handler.wfile.flush()
    return True


def encode_frame(payload: bytes, opcode: int = OPCODE_TEXT) -> bytes:
    """Encode a server-to-client WebSocket frame (unmasked per RFC 6455)."""
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length <= 125:
        header.append(length)
    elif length <= 65535:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def send_ws_text(sock: socket.socket, text: str, send_lock: Optional[threading.Lock] = None) -> None:
    frame = encode_frame(text.encode("utf-8"), opcode=OPCODE_TEXT)
    if send_lock is not None:
        with send_lock:
            sock.sendall(frame)
    else:
        sock.sendall(frame)


def send_ws_pong(sock: socket.socket, payload: bytes = b"", send_lock: Optional[threading.Lock] = None) -> None:
    frame = encode_frame(payload[:125], opcode=OPCODE_PONG)
    if send_lock is not None:
        with send_lock:
            sock.sendall(frame)
    else:
        sock.sendall(frame)


def send_ws_close(sock: socket.socket, send_lock: Optional[threading.Lock] = None) -> None:
    frame = encode_frame(b"", opcode=OPCODE_CLOSE)
    try:
        if send_lock is not None:
            with send_lock:
                sock.sendall(frame)
        else:
            sock.sendall(frame)
    except OSError:
        pass


def _recv_exact(sock: socket.socket, size: int) -> Optional[bytes]:
    chunks = bytearray()
    while len(chunks) < size:
        try:
            part = sock.recv(size - len(chunks))
        except socket.timeout:
            raise
        except OSError:
            return None
        if not part:
            return None
        chunks.extend(part)
    return bytes(chunks)


def read_ws_frame(sock: socket.socket) -> Optional[Tuple[int, bytes]]:
    """
    Read a single WebSocket frame from client socket.
    Raises `socket.timeout` if socket times out before first header byte.
    Returns `(opcode, unmasked_payload)` or `None` on EOF/error.
    """
    hdr = _recv_exact(sock, 2)
    if hdr is None:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F

    if length == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]

    # Cap pathological frames at 1 MB
    if length > 1_048_576:
        return None

    mask_key = b""
    if masked:
        mask_key = _recv_exact(sock, 4) or b""
        if len(mask_key) != 4:
            return None

    payload = _recv_exact(sock, length) if length > 0 else b""
    if payload is None:
        return None

    if masked and payload:
        unmasked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    else:
        unmasked = payload

    return opcode, unmasked


class AnySocketHandler:  # pragma: no cover - typing helper
    headers: dict
    wfile: object


__all__ = [
    "OPCODE_BINARY",
    "OPCODE_CLOSE",
    "OPCODE_CONTINUATION",
    "OPCODE_PING",
    "OPCODE_PONG",
    "OPCODE_TEXT",
    "compute_accept_key",
    "encode_frame",
    "perform_handshake",
    "read_ws_frame",
    "send_ws_close",
    "send_ws_pong",
    "send_ws_text",
]
