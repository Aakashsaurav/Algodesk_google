"""
core/broker_ws.py
=================
Native Persistent Broker WebSocket Market Data Feed for Upstox v3 & Zerodha Kite.

Architecture:
- Browser <-> AlgoDesk Server: WebSocket (`/ws/live`, `/ws/live-screener`).
- AlgoDesk Server <-> Broker (Upstox v3 / Zerodha Kite): Persistent outbound
  TLS WebSocket (`wss://`) streaming real-time push ticks — eliminating
  repeated REST quote API polling!

Key Features:
1. Zero third-party dependencies required:
   - Uses `websocket-client` if installed, otherwise uses our built-in RFC-6455
     TLS WebSocket client (`StdlibWSClient`) over Python stdlib `socket` + `ssl`.
   - Uses `upstox_client`'s compiled protobuf if installed, otherwise uses our
     pure-Python Protobuf3 wire decoder (`decode_upstox_feed_response`) matching
     Upstox `MarketDataFeedV3.proto` field-for-field.
   - Includes a complete Zerodha KiteTicker big-endian binary packet decoder
     (`decode_zerodha_binary_packets`) supporting 8-byte (LTP), 28/32-byte
     (Index), 44-byte (Quote), and 184-byte (Full + 5-level Depth) packets.
2. Scoped subscription multiplexing (`watchlist` + `live_screen`):
   - Live Terminal and Live Screen share a single persistent broker WebSocket
     connection per active broker, with incremental subscribe/unsubscribe frames
     and automatic reconnect with exponential backoff.
3. Seamless fallback:
   - If the broker WebSocket is connecting, disconnected, or the market is
     closed (no ticks pushed yet), callers can check `is_fresh()` /
     `get_cached_quotes()` and gracefully perform a one-off REST snapshot
     fallback without breaking any existing workflows or offline tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from broker.upstox.auth import auth_manager as upstox_auth
from broker.zerodha.auth import zerodha_auth_manager as zerodha_auth
from config import IST_TZ, config
from core.instruments import InstrumentIndex, instrument_index

logger = logging.getLogger(__name__)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_ORIG_URLOPEN = urllib.request.urlopen


# ── Pure-Python RFC-6455 TLS WebSocket Client ────────────────────────────────


class WebSocketClosedError(RuntimeError):
    """Raised when a remote WebSocket peer closes the connection cleanly."""


class StdlibWSClient:
    """
    Minimal, RFC-6455 compliant TLS WebSocket client using Python stdlib only.
    Supports `wss://` and `ws://`, HTTP 3xx redirects during handshake,
    masked client text/binary frames, fragmented server frames, and Ping/Pong.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._closed = False
        self._buf = bytearray()

    def connect(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        max_redirects: int = 5,
    ) -> None:
        cur_url = url
        for _ in range(max_redirects + 1):
            parsed = urllib.parse.urlsplit(cur_url)
            scheme = parsed.scheme.lower()
            if scheme not in {"ws", "wss", "http", "https"}:
                raise ValueError(f"Unsupported WebSocket URL scheme: {scheme}")
            use_tls = scheme in {"wss", "https"}
            host = parsed.hostname or ""
            port = parsed.port or (443 if use_tls else 80)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            raw_sock = socket.create_connection((host, port), timeout=self._timeout)
            raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if use_tls:
                ctx = ssl.create_default_context()
                sock: socket.socket = ctx.wrap_socket(raw_sock, server_hostname=host)
            else:
                sock = raw_sock

            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = f"{host}:{port}" if parsed.port else host
            req_headers = {
                "Host": host_header,
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": ws_key,
                "Sec-WebSocket-Version": "13",
                "User-Agent": "AlgoDesk-WS/5.0",
                **(headers or {}),
            }
            lines = [f"GET {path} HTTP/1.1"]
            lines.extend(f"{k}: {v}" for k, v in req_headers.items())
            handshake = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
            sock.sendall(handshake)

            # Read HTTP upgrade response headers
            resp_buf = bytearray()
            while b"\r\n\r\n" not in resp_buf:
                chunk = sock.recv(4096)
                if not chunk:
                    sock.close()
                    raise ConnectionError("WebSocket server closed connection during handshake")
                resp_buf.extend(chunk)
                if len(resp_buf) > 65536:
                    sock.close()
                    raise ConnectionError("WebSocket handshake header too large")

            head_end = resp_buf.index(b"\r\n\r\n")
            head_bytes = bytes(resp_buf[:head_end])
            leftover = bytes(resp_buf[head_end + 4 :])
            head_lines = head_bytes.decode("iso-8859-1", errors="replace").split("\r\n")
            status_line = head_lines[0] if head_lines else ""
            parts = status_line.split(" ", 2)
            status_code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            resp_headers: Dict[str, str] = {}
            for line in head_lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    resp_headers[k.strip().lower()] = v.strip()

            if status_code in {301, 302, 303, 307, 308} and "location" in resp_headers:
                sock.close()
                cur_url = urllib.parse.urljoin(cur_url, resp_headers["location"])
                continue

            if status_code != 101:
                sock.close()
                raise ConnectionError(f"WebSocket handshake failed: {status_line}")

            expected_accept = base64.b64encode(
                hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            got_accept = resp_headers.get("sec-websocket-accept", "")
            if got_accept and got_accept != expected_accept:
                sock.close()
                raise ConnectionError("WebSocket handshake Sec-WebSocket-Accept mismatch")

            self._sock = sock
            self._closed = False
            self._buf = bytearray(leftover)
            return

        raise ConnectionError("Too many redirects during WebSocket handshake")

    def set_timeout(self, timeout: float) -> None:
        self._timeout = timeout
        if self._sock is not None:
            self._sock.settimeout(timeout)

    def _read_exact(self, n: int) -> bytes:
        assert self._sock is not None
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WebSocketClosedError("WebSocket EOF")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._closed or self._sock is None:
                raise WebSocketClosedError("WebSocket is closed")
            first_byte = 0x80 | (opcode & 0x0F)
            mask_key = os.urandom(4)
            length = len(payload)
            header = bytearray([first_byte])
            if length < 126:
                header.append(0x80 | length)
            elif length < 65536:
                header.append(0x80 | 126)
                header.extend(struct.pack("!H", length))
            else:
                header.append(0x80 | 127)
                header.extend(struct.pack("!Q", length))
            header.extend(mask_key)
            masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
            self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(0x2, data)

    def send_ping(self, payload: bytes = b"") -> None:
        self._send_frame(0x9, payload[:125])

    def recv(self) -> Tuple[int, bytes]:
        """
        Receive the next complete data frame `(opcode, payload)`.
        `opcode == 0x1` for text, `0x2` for binary.
        Automatically replies to Ping (0x9) with Pong (0xA) and raises
        `WebSocketClosedError` on Close (0x8) or EOF.
        """
        frag_opcode: Optional[int] = None
        frag_parts: List[bytes] = []

        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read_exact(8))
            mask_key = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length > 0 else b""
            if masked and mask_key:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                try:
                    self._send_frame(0x8, b"")
                except Exception:
                    pass
                self.close()
                raise WebSocketClosedError("WebSocket closed by remote peer")
            if opcode == 0x9:  # Ping -> send Pong immediately
                try:
                    self._send_frame(0xA, payload[:125])
                except Exception:
                    pass
                continue
            if opcode == 0xA:  # Pong
                continue

            if opcode in {0x1, 0x2}:
                if fin:
                    return opcode, payload
                frag_opcode = opcode
                frag_parts = [payload]
            elif opcode == 0x0 and frag_opcode is not None:
                frag_parts.append(payload)
                if fin:
                    out_op = frag_opcode
                    out_data = b"".join(frag_parts)
                    frag_opcode = None
                    frag_parts = []
                    return out_op, out_data

    def close(self) -> None:
        self._closed = True
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# ── Pure-Python Protobuf3 Wire Decoder for Upstox MarketDataFeedV3 ───────────


def _pb_read_varint(buf: bytes, pos: int, end: int) -> Tuple[int, int]:
    val = 0
    shift = 0
    while pos < end:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 70:
            raise ValueError("Invalid protobuf varint")
    raise ValueError("Truncated protobuf varint")


def _pb_iter_fields(buf: bytes, start: int = 0, end: Optional[int] = None) -> Iterable[Tuple[int, int, Any]]:
    """Yield `(field_number, wire_type, raw_value)` from a protobuf wire buffer."""
    pos = start
    limit = len(buf) if end is None else end
    while pos < limit:
        tag, pos = _pb_read_varint(buf, pos, limit)
        field_no = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            val, pos = _pb_read_varint(buf, pos, limit)
            yield field_no, wire_type, val
        elif wire_type == 1:  # 64-bit fixed
            if pos + 8 > limit:
                break
            raw = buf[pos : pos + 8]
            pos += 8
            yield field_no, wire_type, raw
        elif wire_type == 2:  # length-delimited
            ln, pos = _pb_read_varint(buf, pos, limit)
            if pos + ln > limit:
                break
            raw = buf[pos : pos + ln]
            pos += ln
            yield field_no, wire_type, raw
        elif wire_type == 5:  # 32-bit fixed
            if pos + 4 > limit:
                break
            raw = buf[pos : pos + 4]
            pos += 4
            yield field_no, wire_type, raw
        else:
            break


def _pb_double(wire_type: int, raw: Any) -> float:
    if wire_type == 1 and isinstance(raw, (bytes, bytearray)) and len(raw) == 8:
        return struct.unpack("<d", raw)[0]
    if wire_type == 5 and isinstance(raw, (bytes, bytearray)) and len(raw) == 4:
        return float(struct.unpack("<f", raw)[0])
    return 0.0


def _pb_int64(val: int) -> int:
    # Handle signed two's-complement 64-bit varint if negative
    if val >= (1 << 63):
        val -= 1 << 64
    return int(val)


def _decode_upstox_ltpc(buf: bytes) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1:
            out["ltp"] = _pb_double(wt, raw)
        elif fno == 2 and wt == 0:
            out["ltt"] = _pb_int64(raw)
        elif fno == 3 and wt == 0:
            out["ltq"] = _pb_int64(raw)
        elif fno == 4:
            out["cp"] = _pb_double(wt, raw)
    return out


def _decode_upstox_quote(buf: bytes) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 0:
            out["bidQ"] = _pb_int64(raw)
        elif fno == 2:
            out["bidP"] = _pb_double(wt, raw)
        elif fno == 3 and wt == 0:
            out["askQ"] = _pb_int64(raw)
        elif fno == 4:
            out["askP"] = _pb_double(wt, raw)
    return out


def _decode_upstox_market_level(buf: bytes) -> List[Dict[str, Any]]:
    levels: List[Dict[str, Any]] = []
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            levels.append(_decode_upstox_quote(raw))
    return levels


def _decode_upstox_ohlc_item(buf: bytes) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            out["interval"] = raw.decode("utf-8", errors="ignore")
        elif fno == 2:
            out["open"] = _pb_double(wt, raw)
        elif fno == 3:
            out["high"] = _pb_double(wt, raw)
        elif fno == 4:
            out["low"] = _pb_double(wt, raw)
        elif fno == 5:
            out["close"] = _pb_double(wt, raw)
        elif fno == 6 and wt == 0:
            out["vol"] = _pb_int64(raw)
        elif fno == 7 and wt == 0:
            out["ts"] = _pb_int64(raw)
    return out


def _decode_upstox_market_ohlc(buf: bytes) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            items.append(_decode_upstox_ohlc_item(raw))
    return items


def _decode_upstox_market_ff(buf: bytes) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            out["ltpc"] = _decode_upstox_ltpc(raw)
        elif fno == 2 and wt == 2:
            out["depth"] = _decode_upstox_market_level(raw)
        elif fno == 4 and wt == 2:
            out["ohlc"] = _decode_upstox_market_ohlc(raw)
        elif fno == 5:
            out["atp"] = _pb_double(wt, raw)
        elif fno == 6 and wt == 0:
            out["vtt"] = _pb_int64(raw)
        elif fno == 7:
            out["oi"] = _pb_double(wt, raw)
        elif fno == 8:
            out["tbq"] = int(_pb_int64(raw) if wt == 0 else _pb_double(wt, raw))
        elif fno == 9:
            out["tsq"] = int(_pb_int64(raw) if wt == 0 else _pb_double(wt, raw))
    return out


def _decode_upstox_index_ff(buf: bytes) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            out["ltpc"] = _decode_upstox_ltpc(raw)
        elif fno == 2 and wt == 2:
            out["ohlc"] = _decode_upstox_market_ohlc(raw)
    return out


def _decode_upstox_full_feed(buf: bytes) -> Dict[str, Any]:
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            return _decode_upstox_market_ff(raw)
        if fno == 2 and wt == 2:
            return _decode_upstox_index_ff(raw)
    return {}


def _decode_upstox_first_level_greeks(buf: bytes) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            out["ltpc"] = _decode_upstox_ltpc(raw)
        elif fno == 2 and wt == 2:
            out["depth"] = [_decode_upstox_quote(raw)]
        elif fno == 4 and wt == 0:
            out["vtt"] = _pb_int64(raw)
        elif fno == 5:
            out["oi"] = _pb_double(wt, raw)
    return out


def _decode_upstox_feed_entry(buf: bytes) -> Dict[str, Any]:
    for fno, wt, raw in _pb_iter_fields(buf):
        if fno == 1 and wt == 2:
            return {"ltpc": _decode_upstox_ltpc(raw)}
        if fno == 2 and wt == 2:
            return _decode_upstox_full_feed(raw)
        if fno == 3 and wt == 2:
            return _decode_upstox_first_level_greeks(raw)
    return {}


def _ms_to_ist(ms_val: Optional[int], *, allow_fallback_now: bool = True) -> Optional[str]:
    if ms_val and ms_val > 1_000_000_000_000:
        try:
            return datetime.fromtimestamp(ms_val / 1000.0, tz=timezone.utc).astimezone(IST_TZ).isoformat(timespec="seconds")
        except Exception:
            pass
    elif ms_val and ms_val > 1_000_000_000:
        try:
            return datetime.fromtimestamp(float(ms_val), tz=timezone.utc).astimezone(IST_TZ).isoformat(timespec="seconds")
        except Exception:
            pass
    if allow_fallback_now:
        return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
    return None


def decode_upstox_feed_response(
    payload: bytes,
    key_meta_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Optional[int]]:
    """
    Decode an Upstox v3 binary Protobuf `FeedResponse` frame into
    `({SYMBOL: normalized_quote_dict}, current_ts_ms)`.

    Works natively with zero external dependencies using our Protobuf3 wire
    decoder (matching `com.upstox.marketdatafeeder.rpc.proto.FeedResponse`).
    """
    key_meta = key_meta_map or {}
    upper_meta = {k.upper(): v for k, v in key_meta.items()}
    current_ts_ms: Optional[int] = None
    raw_feeds: Dict[str, Dict[str, Any]] = {}

    for fno, wt, raw in _pb_iter_fields(payload):
        if fno == 3 and wt == 0:
            current_ts_ms = _pb_int64(raw)
        elif fno == 2 and wt == 2:
            # Protobuf map<string, Feed> entry: field 1 = key (string), field 2 = value (Feed)
            ikey = ""
            entry_data: Dict[str, Any] = {}
            for mfno, mwt, mraw in _pb_iter_fields(raw):
                if mfno == 1 and mwt == 2:
                    ikey = mraw.decode("utf-8", errors="ignore")
                elif mfno == 2 and mwt == 2:
                    entry_data = _decode_upstox_feed_entry(mraw)
            if ikey and entry_data:
                raw_feeds[ikey] = entry_data

    quotes: Dict[str, Dict[str, Any]] = {}
    for ikey, data in raw_feeds.items():
        ltpc = data.get("ltpc") or {}
        raw_ltp = ltpc.get("ltp")
        if raw_ltp is None or float(raw_ltp) <= 0:
            continue

        meta = key_meta.get(ikey) or upper_meta.get(ikey.upper()) or {}
        sym = str(meta.get("symbol") or ikey.split("|")[-1]).strip().upper()
        exch = str(meta.get("exchange") or "NSE").strip().upper()
        itype = str(meta.get("instrument_type") or "EQUITY").strip().upper()

        ltp = round(float(raw_ltp), 2)
        cp = ltpc.get("cp")
        prev_close = round(float(cp), 2) if (cp is not None and float(cp) > 0) else None

        ohlc_list = data.get("ohlc") or []
        day_ohlc = next((o for o in ohlc_list if str(o.get("interval") or "").lower() in {"1d", "d1", "day"}), None)
        if day_ohlc is None and ohlc_list:
            day_ohlc = ohlc_list[0]

        o_val = day_ohlc.get("open") if day_ohlc and day_ohlc.get("open") else ltp
        h_val = day_ohlc.get("high") if day_ohlc and day_ohlc.get("high") else ltp
        l_val = day_ohlc.get("low") if day_ohlc and day_ohlc.get("low") else ltp

        vol_val = data.get("vtt")
        if not vol_val and day_ohlc and day_ohlc.get("vol"):
            vol_val = day_ohlc.get("vol")

        raw_depth = data.get("depth") or []
        bids_list: List[Dict[str, Any]] = []
        asks_list: List[Dict[str, Any]] = []
        for lvl in raw_depth:
            if not isinstance(lvl, dict):
                continue
            bp = float(lvl.get("bidP") or 0.0)
            bq = int(lvl.get("bidQ") or 0)
            ap = float(lvl.get("askP") or 0.0)
            aq = int(lvl.get("askQ") or 0)
            if bp > 0 or bq > 0:
                bids_list.append({"price": round(bp, 4), "quantity": bq, "orders": 0})
            if ap > 0 or aq > 0:
                asks_list.append({"price": round(ap, 4), "quantity": aq, "orders": 0})

        bid_val = round(bids_list[0]["price"], 2) if (bids_list and bids_list[0]["price"] > 0) else None
        ask_val = round(asks_list[0]["price"], 2) if (asks_list and asks_list[0]["price"] > 0) else None

        net_chg = (ltp - prev_close) if (prev_close is not None and prev_close > 0) else None
        chg_pct = round((net_chg / prev_close) * 100.0, 2) if (net_chg is not None and prev_close) else 0.0

        ts_ms = ltpc.get("ltt") or current_ts_ms
        source_ts = _ms_to_ist(ts_ms, allow_fallback_now=False)
        received_ts = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        tbq = int(data.get("tbq") or sum(b["quantity"] for b in bids_list))
        tsq = int(data.get("tsq") or sum(a["quantity"] for a in asks_list))
        depth_payload: Optional[Dict[str, Any]] = None
        if bids_list or asks_list:
            spread_val = round(ask_val - bid_val, 4) if (bid_val is not None and ask_val is not None) else None
            mid_val = ((bid_val + ask_val) / 2.0) if (bid_val is not None and ask_val is not None and (bid_val + ask_val) > 0) else None
            spread_bps = round((spread_val / mid_val) * 10000.0, 2) if (spread_val is not None and mid_val) else None
            tot_q = tbq + tsq
            imbalance = round((tbq - tsq) / float(tot_q), 4) if tot_q > 0 else 0.0
            depth_payload = {
                "symbol": sym,
                "exchange": exch,
                "depth_levels": max(len(bids_list), len(asks_list)),
                "bids": bids_list,
                "asks": asks_list,
                "best_bid": bid_val,
                "best_ask": ask_val,
                "spread": spread_val,
                "spread_bps": spread_bps,
                "total_buy_qty": tbq,
                "total_sell_qty": tsq,
                "imbalance_ratio": imbalance,
                "source": "upstox",
                "transport": "broker_websocket",
                "ts_ist": source_ts or received_ts,
            }

        quotes[sym] = {
            "symbol": sym,
            "exchange": exch,
            "instrument_type": itype,
            "instrument_key": ikey,
            "ltp": ltp,
            "prev_close": prev_close,
            "open": round(float(o_val), 2),
            "high": round(float(h_val), 2),
            "low": round(float(l_val), 2),
            "change": round(float(net_chg), 2) if net_chg is not None else None,
            "change_pct": chg_pct,
            "volume": int(vol_val or 0),
            "oi": int(float(data.get("oi") or 0)),
            "bid": bid_val,
            "ask": ask_val,
            "depth": depth_payload,
            "depth_levels": depth_payload["depth_levels"] if depth_payload else 0,
            "source": "upstox",
            "transport": "broker_websocket",
            "execution_grade": True,
            "source_timestamp_ist": source_ts,
            "received_at_ist": received_ts,
            "note": "",
            "ts_ist": source_ts or received_ts,
        }

    return quotes, current_ts_ms


# ── Pure-Python Binary Packet Decoder for Zerodha KiteTicker ─────────────────


def decode_zerodha_binary_packets(
    payload: bytes,
    token_meta_map: Optional[Dict[int, Dict[str, str]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Decode a Zerodha KiteTicker binary WebSocket frame into
    `{SYMBOL: normalized_quote_dict}`.

    Supports official Kite packet lengths:
    - 8 bytes: LTP mode
    - 28 / 32 bytes: Index Quote / Full mode
    - 44 bytes: Tradable Quote mode
    - 184 bytes: Tradable Full mode (including OI, timestamps, 5-level depth)
    """
    if not payload or len(payload) < 4:
        return {}

    token_meta = token_meta_map or {}
    (pkt_count,) = struct.unpack("!H", payload[0:2])
    offset = 2
    now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
    quotes: Dict[str, Dict[str, Any]] = {}

    for _ in range(pkt_count):
        if offset + 2 > len(payload):
            break
        (pkt_len,) = struct.unpack("!H", payload[offset : offset + 2])
        offset += 2
        if offset + pkt_len > len(payload):
            break
        pkt = payload[offset : offset + pkt_len]
        offset += pkt_len
        if pkt_len < 8:
            continue

        (token,) = struct.unpack("!I", pkt[0:4])
        segment = token & 0xFF
        divisor = 10_000_000.0 if segment == 3 else (10_000.0 if segment == 6 else 100.0)

        meta = token_meta.get(token) or {}
        sym = str(meta.get("symbol") or str(token)).strip().upper()
        exch = str(meta.get("exchange") or "NSE").strip().upper()
        itype = str(meta.get("instrument_type") or ("INDEX" if pkt_len in {28, 32} else "EQUITY")).strip().upper()

        ltp = round(struct.unpack("!I", pkt[4:8])[0] / divisor, 2)
        if ltp <= 0:
            continue

        prev_close: Optional[float] = None
        o_val = ltp
        h_val = ltp
        l_val = ltp
        vol_val = 0
        oi_val = 0
        bid_val: Optional[float] = None
        ask_val: Optional[float] = None
        tbq_val = 0
        tsq_val = 0
        bids_list: List[Dict[str, Any]] = []
        asks_list: List[Dict[str, Any]] = []
        source_ts: Optional[str] = None
        ts_str = now_ist

        if pkt_len in {28, 32}:
            # Index packet: token(0), ltp(4), high(8), low(12), open(16), close(20), change(24), [exch_ts(28)]
            h_val = round(struct.unpack("!I", pkt[8:12])[0] / divisor, 2) or ltp
            l_val = round(struct.unpack("!I", pkt[12:16])[0] / divisor, 2) or ltp
            o_val = round(struct.unpack("!I", pkt[16:20])[0] / divisor, 2) or ltp
            cp_raw = struct.unpack("!I", pkt[20:24])[0] / divisor
            prev_close = round(cp_raw, 2) if cp_raw > 0 else None
            if pkt_len == 32:
                exch_ts = struct.unpack("!I", pkt[28:32])[0]
                if exch_ts > 1_000_000_000:
                    source_ts = _ms_to_ist(exch_ts, allow_fallback_now=False)
                    if source_ts:
                        ts_str = source_ts
        elif pkt_len >= 44:
            # Tradable Quote/Full: token(0), ltp(4), ltq(8), atp(12), vol(16), tbq(20), tsq(24),
            # open(28), high(32), low(36), close(40)
            vol_val = int(struct.unpack("!I", pkt[16:20])[0])
            tbq_val = int(struct.unpack("!I", pkt[20:24])[0])
            tsq_val = int(struct.unpack("!I", pkt[24:28])[0])
            o_val = round(struct.unpack("!I", pkt[28:32])[0] / divisor, 2) or ltp
            h_val = round(struct.unpack("!I", pkt[32:36])[0] / divisor, 2) or ltp
            l_val = round(struct.unpack("!I", pkt[36:40])[0] / divisor, 2) or ltp
            cp_raw = struct.unpack("!I", pkt[40:44])[0] / divisor
            prev_close = round(cp_raw, 2) if cp_raw > 0 else None

            if pkt_len >= 184:
                ltt = struct.unpack("!I", pkt[44:48])[0]
                oi_val = int(struct.unpack("!I", pkt[48:52])[0])
                exch_ts = struct.unpack("!I", pkt[60:64])[0]
                chosen_ts = exch_ts if exch_ts > 1_000_000_000 else ltt
                if chosen_ts > 1_000_000_000:
                    source_ts = _ms_to_ist(chosen_ts, allow_fallback_now=False)
                    if source_ts:
                        ts_str = source_ts
                # Market depth: 5 buy entries (64..124) + 5 sell entries (124..184), 12 bytes each: qty(I), price(I), orders(H), pad(2x)
                for lvl_i in range(5):
                    b_off = 64 + lvl_i * 12
                    b_qty, b_px_raw, b_orders = struct.unpack("!IIH2x", pkt[b_off : b_off + 12])
                    b_px = round(b_px_raw / divisor, 4)
                    if b_px > 0 or b_qty > 0:
                        bids_list.append({"price": b_px, "quantity": int(b_qty), "orders": int(b_orders)})
                    a_off = 124 + lvl_i * 12
                    a_qty, a_px_raw, a_orders = struct.unpack("!IIH2x", pkt[a_off : a_off + 12])
                    a_px = round(a_px_raw / divisor, 4)
                    if a_px > 0 or a_qty > 0:
                        asks_list.append({"price": a_px, "quantity": int(a_qty), "orders": int(a_orders)})
                if bids_list and bids_list[0]["price"] > 0:
                    bid_val = round(bids_list[0]["price"], 2)
                if asks_list and asks_list[0]["price"] > 0:
                    ask_val = round(asks_list[0]["price"], 2)

        net_chg = (ltp - prev_close) if (prev_close is not None and prev_close > 0) else None
        chg_pct = round((net_chg / prev_close) * 100.0, 2) if (net_chg is not None and prev_close) else 0.0

        depth_payload: Optional[Dict[str, Any]] = None
        if bids_list or asks_list:
            spread_val = round(ask_val - bid_val, 4) if (bid_val is not None and ask_val is not None) else None
            mid_val = ((bid_val + ask_val) / 2.0) if (bid_val is not None and ask_val is not None and (bid_val + ask_val) > 0) else None
            spread_bps = round((spread_val / mid_val) * 10000.0, 2) if (spread_val is not None and mid_val) else None
            tot_q = tbq_val + tsq_val
            imbalance = round((tbq_val - tsq_val) / float(tot_q), 4) if tot_q > 0 else 0.0
            depth_payload = {
                "symbol": sym,
                "exchange": exch,
                "depth_levels": max(len(bids_list), len(asks_list)),
                "bids": bids_list,
                "asks": asks_list,
                "best_bid": bid_val,
                "best_ask": ask_val,
                "spread": spread_val,
                "spread_bps": spread_bps,
                "total_buy_qty": tbq_val,
                "total_sell_qty": tsq_val,
                "imbalance_ratio": imbalance,
                "source": "zerodha",
                "transport": "broker_websocket",
                "ts_ist": source_ts or ts_str,
            }

        quotes[sym] = {
            "symbol": sym,
            "exchange": exch,
            "instrument_type": itype,
            "instrument_key": str(token),
            "ltp": ltp,
            "prev_close": prev_close,
            "open": o_val,
            "high": h_val,
            "low": l_val,
            "change": round(float(net_chg), 2) if net_chg is not None else None,
            "change_pct": chg_pct,
            "volume": vol_val,
            "oi": oi_val,
            "bid": bid_val,
            "ask": ask_val,
            "depth": depth_payload,
            "depth_levels": depth_payload["depth_levels"] if depth_payload else 0,
            "source": "zerodha",
            "transport": "broker_websocket",
            "execution_grade": True,
            "source_timestamp_ist": source_ts,
            "received_at_ist": now_ist,
            "note": "",
            "ts_ist": ts_str,
        }

    return quotes


# ── Persistent Scoped Broker WebSocket Streamers ─────────────────────────────


class _BaseBrokerWSClient:
    """
    Base class for a persistent, auto-reconnecting outbound broker WebSocket
    with scoped subscription multiplexing (`watchlist`, `live_screen`, etc.).
    """

    broker_id: str = ""

    def __init__(
        self,
        *,
        instruments: Optional[InstrumentIndex] = None,
        on_ticks: Optional[Callable[[str, Dict[str, Dict[str, Any]], Optional[float]], None]] = None,
    ) -> None:
        self._instruments = instruments or instrument_index
        self._on_ticks = on_ticks
        self._lock = threading.RLock()
        self._scopes: Dict[str, List[Dict[str, str]]] = {}
        self._active_keys: Set[Any] = set()
        self._key_meta: Dict[Any, Dict[str, str]] = {}
        self._latest_quotes: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        self._ws: Optional[StdlibWSClient] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_ev = threading.Event()
        self._sub_dirty = threading.Event()

        # Health telemetry
        self._connected = False
        self._connecting = False
        self._connected_mono: float = 0.0
        self._last_tick_mono: float = 0.0
        self._latency_ms: Optional[float] = None
        self._messages_received = 0
        self._ticks_received = 0
        self._reconnect_count = 0
        self._last_error = ""

    def set_scope_subscriptions(self, scope: str, subs: List[Dict[str, str]]) -> None:
        cleaned: List[Dict[str, str]] = []
        for item in subs or []:
            if isinstance(item, dict) and item.get("symbol"):
                cleaned.append(
                    {
                        "symbol": str(item["symbol"]).strip().upper(),
                        "exchange": str(item.get("exchange") or "NSE").strip().upper(),
                        "instrument_type": str(item.get("instrument_type") or "EQUITY").strip().upper(),
                        "instrument_key": str(item.get("instrument_key") or "").strip(),
                    }
                )
        with self._lock:
            self._scopes[scope] = cleaned
            self._sub_dirty.set()
        if cleaned:
            self.start()

    def release_scope(self, scope: str) -> None:
        with self._lock:
            removed = self._scopes.pop(scope, None)
            if removed is not None:
                self._sub_dirty.set()
            has_any = any(bool(v) for v in self._scopes.values())
        if not has_any:
            self.stop()

    def _merged_subscriptions_locked(self) -> List[Dict[str, str]]:
        seen: Set[Tuple[str, str]] = set()
        merged: List[Dict[str, str]] = []
        for subs in self._scopes.values():
            for s in subs:
                k = (s["symbol"], s["exchange"])
                if k not in seen:
                    seen.add(k)
                    merged.append(dict(s))
        return merged

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_ev.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=f"algodesk-broker-ws-{self.broker_id}",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_ev.set()
            ws = self._ws
            self._connected = False
            self._connecting = False
        if ws is not None:
            ws.close()

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def get_cached_quotes(
        self,
        symbols: Iterable[str],
        *,
        max_age_sec: float = 30.0,
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[float]]:
        now = time.monotonic()
        wanted = {str(s).strip().upper() for s in symbols if str(s).strip()}
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            lat = self._latency_ms
            for sym in wanted:
                entry = self._latest_quotes.get(sym)
                if entry is not None:
                    ts_mono, quote = entry
                    if (now - ts_mono) <= max_age_sec:
                        out[sym] = dict(quote)
        return out, lat

    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            age = round(now - self._last_tick_mono, 1) if self._last_tick_mono > 0 else None
            return {
                "broker_id": self.broker_id,
                "connected": self._connected,
                "connecting": self._connecting,
                "transport": "broker_websocket" if self._connected else "disconnected",
                "subscribed_count": len(self._active_keys),
                "messages_received": self._messages_received,
                "ticks_received": self._ticks_received,
                "reconnect_count": self._reconnect_count,
                "latency_ms": self._latency_ms,
                "last_tick_age_sec": age,
                "last_error": self._last_error,
            }

    # Subclass hooks
    def _build_connection_params(self) -> Tuple[str, Dict[str, str]]:
        raise NotImplementedError

    def _resolve_keys(self, subs: List[Dict[str, str]]) -> Dict[Any, Dict[str, str]]:
        raise NotImplementedError

    def _send_subscribe(self, ws: StdlibWSClient, keys: List[Any]) -> None:
        raise NotImplementedError

    def _send_unsubscribe(self, ws: StdlibWSClient, keys: List[Any]) -> None:
        raise NotImplementedError

    def _handle_frame(self, opcode: int, payload: bytes) -> Tuple[Dict[str, Dict[str, Any]], Optional[float]]:
        raise NotImplementedError

    def _sync_subscriptions(self, ws: StdlibWSClient) -> None:
        with self._lock:
            subs = self._merged_subscriptions_locked()
            self._sub_dirty.clear()
        desired_map = self._resolve_keys(subs)
        desired_keys = set(desired_map.keys())
        with self._lock:
            to_add = [k for k in desired_keys if k not in self._active_keys]
            to_remove = [k for k in self._active_keys if k not in desired_keys]
            self._key_meta = desired_map
        if to_remove:
            self._send_unsubscribe(ws, to_remove)
            with self._lock:
                for k in to_remove:
                    self._active_keys.discard(k)
        if to_add:
            self._send_subscribe(ws, to_add)
            with self._lock:
                self._active_keys.update(to_add)

    def _ingest_ticks(self, quotes: Dict[str, Dict[str, Any]], latency_ms: Optional[float]) -> None:
        if not quotes:
            return
        now = time.monotonic()
        with self._lock:
            self._last_tick_mono = now
            self._ticks_received += len(quotes)
            if latency_ms is not None and 0.0 <= latency_ms <= 60_000.0:
                self._latency_ms = round(latency_ms, 1)
            for sym, q in quotes.items():
                self._latest_quotes[sym] = (now, dict(q))
            cb = self._on_ticks
            eff_lat = self._latency_ms
        if cb is not None:
            try:
                cb(self.broker_id, quotes, eff_lat)
            except Exception as exc:
                logger.debug("Broker WS on_ticks callback error (%s): %s", self.broker_id, exc)

    def _run_loop(self) -> None:
        backoff = 2.0
        while not self._stop_ev.is_set():
            with self._lock:
                has_subs = bool(self._merged_subscriptions_locked())
            if not has_subs:
                self._stop_ev.wait(1.0)
                continue

            ws = StdlibWSClient(timeout=10.0)
            try:
                with self._lock:
                    self._connecting = True
                    self._connected = False
                    self._active_keys.clear()
                url, headers = self._build_connection_params()
                t0 = time.perf_counter()
                ws.connect(url, headers=headers)
                connect_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                ws.set_timeout(1.5)
                with self._lock:
                    self._ws = ws
                    self._connected = True
                    self._connecting = False
                    self._connected_mono = time.monotonic()
                    self._last_error = ""
                    if self._latency_ms is None:
                        self._latency_ms = connect_ms
                backoff = 2.0
                logger.info("Connected to %s broker WebSocket (handshake %.1f ms)", self.broker_id.upper(), connect_ms)

                self._sync_subscriptions(ws)

                last_ping = time.monotonic()
                while not self._stop_ev.is_set():
                    if self._sub_dirty.is_set():
                        self._sync_subscriptions(ws)
                    try:
                        opcode, payload = ws.recv()
                        with self._lock:
                            self._messages_received += 1
                        quotes, tick_lat = self._handle_frame(opcode, payload)
                        if quotes:
                            self._ingest_ticks(quotes, tick_lat)
                    except (socket.timeout, TimeoutError):
                        now = time.monotonic()
                        if now - last_ping >= 20.0:
                            try:
                                ws.send_ping(b"ping")
                            except Exception:
                                break
                            last_ping = now
                        continue
            except Exception as exc:
                err_msg = str(exc)
                with self._lock:
                    self._connected = False
                    self._connecting = False
                    self._last_error = err_msg
                    self._reconnect_count += 1
                    self._ws = None
                logger.debug("%s broker WebSocket disconnected: %s (retry in %.1fs)", self.broker_id, err_msg, backoff)
            finally:
                ws.close()
                with self._lock:
                    if self._ws is ws:
                        self._ws = None
                    self._connected = False
                    self._connecting = False

            if not self._stop_ev.is_set():
                self._stop_ev.wait(backoff)
                backoff = min(30.0, backoff * 1.7)


class UpstoxWSFeedClient(_BaseBrokerWSClient):
    """
    Upstox v3 Market Data Feed persistent WebSocket streamer (Protobuf3).
    Supports both Level-5 (`mode="full"`) and Level-30 (`mode="full_d30"`)
    order-book streaming with dynamic live mode switching (`change_mode`).
    """

    broker_id = "upstox"
    max_depth_levels = 30

    def __init__(
        self,
        *,
        instruments: Optional[InstrumentIndex] = None,
        on_ticks: Optional[Callable[[str, Dict[str, Dict[str, Any]], Optional[float]], None]] = None,
        feed_mode: str = "full",
        depth_levels: Optional[int] = None,
        on_tick: Any = None,
        on_status: Any = None,
    ) -> None:
        super().__init__(instruments=instruments, on_ticks=on_ticks or on_tick)
        if depth_levels is not None:
            self._feed_mode = "full_d30" if int(depth_levels) > 5 else "full"
        else:
            self._feed_mode = "full_d30" if str(feed_mode or "").strip().lower() in {"full_d30", "d30", "30"} else "full"
        self._depth_levels = 30 if self._feed_mode == "full_d30" else 5

    def set_depth_levels(self, levels: int) -> int:
        """Dynamically switch Upstox v3 stream between Level-5 (`full`) and Level-30 (`full_d30`)."""
        target_levels = 30 if int(levels or 5) > 5 else 5
        target_mode = "full_d30" if target_levels == 30 else "full"
        with self._lock:
            changed = target_mode != self._feed_mode
            self._feed_mode = target_mode
            self._depth_levels = target_levels
            ws = self._ws
            active = list(self._active_keys)
        if changed and ws is not None and active:
            try:
                for i in range(0, len(active), 100):
                    batch = active[i : i + 100]
                    msg = {
                        "guid": f"algodesk-{uuid.uuid4().hex[:10]}",
                        "method": "change_mode",
                        "data": {"mode": target_mode, "instrumentKeys": batch},
                    }
                    ws.send_binary(json.dumps(msg).encode("utf-8"))
            except Exception as exc:
                logger.debug("Upstox WS change_mode(%s) failed: %s", target_mode, exc)
                self._sub_dirty.set()
        return target_levels

    def status(self) -> Dict[str, Any]:
        st = super().status()
        with self._lock:
            st["feed_mode"] = self._feed_mode
            st["depth_levels"] = self._depth_levels
            st["max_depth_levels"] = self.max_depth_levels
        return st

    def _build_connection_params(self) -> Tuple[str, Dict[str, str]]:
        token = upstox_auth.get_valid_token()
        auth_url = f"{config.UPSTOX_BASE_URL}/v3/feed/market-data-feed/authorize"
        req = urllib.request.Request(
            auth_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "AlgoDesk/5.0",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ws_url = (
            (body.get("data") or {}).get("authorized_redirect_uri")
            or (body.get("data") or {}).get("authorizedRedirectUri")
            or ""
        )
        if not ws_url:
            raise RuntimeError("Upstox v3 authorize did not return an authorizedRedirectUri.")
        return ws_url, {}

    def _resolve_keys(self, subs: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        for item in subs:
            sym = item["symbol"]
            exch = item.get("exchange") or "NSE"
            itype = item.get("instrument_type") or ""
            ikey = item.get("instrument_key") or ""
            if ikey and "|" in ikey:
                resolved = ikey
            else:
                resolved = self._instruments.resolve_key(
                    sym, broker_id="upstox", exchange=exch, instrument_type=itype or None
                ) or ""
            if resolved:
                out[resolved] = {"symbol": sym, "exchange": exch, "instrument_type": itype or "EQUITY"}
        return out

    def _send_subscribe(self, ws: StdlibWSClient, keys: List[Any]) -> None:
        # Upstox v3 Market Data Feed expects subscribe JSON as a binary frame (in batches of 100)
        with self._lock:
            mode = self._feed_mode
        for i in range(0, len(keys), 100):
            batch = keys[i : i + 100]
            msg = {
                "guid": f"algodesk-{uuid.uuid4().hex[:10]}",
                "method": "sub",
                "data": {"mode": mode, "instrumentKeys": batch},
            }
            ws.send_binary(json.dumps(msg).encode("utf-8"))

    def _send_unsubscribe(self, ws: StdlibWSClient, keys: List[Any]) -> None:
        with self._lock:
            mode = self._feed_mode
        for i in range(0, len(keys), 100):
            batch = keys[i : i + 100]
            msg = {
                "guid": f"algodesk-{uuid.uuid4().hex[:10]}",
                "method": "unsub",
                "data": {"mode": mode, "instrumentKeys": batch},
            }
            ws.send_binary(json.dumps(msg).encode("utf-8"))

    def _handle_frame(self, opcode: int, payload: bytes) -> Tuple[Dict[str, Dict[str, Any]], Optional[float]]:
        if opcode != 0x2 or not payload:
            return {}, None
        with self._lock:
            key_meta = dict(self._key_meta)
        quotes, feed_ts_ms = decode_upstox_feed_response(payload, key_meta_map=key_meta)
        lat_ms: Optional[float] = None
        if feed_ts_ms and feed_ts_ms > 1_000_000_000_000:
            diff = (time.time() * 1000.0) - float(feed_ts_ms)
            if 0.0 <= diff <= 30_000.0:
                lat_ms = round(diff, 1)
        return quotes, lat_ms


class ZerodhaWSFeedClient(_BaseBrokerWSClient):
    """Zerodha KiteTicker persistent WebSocket streamer (binary packets, Level-5 full depth)."""

    broker_id = "zerodha"
    max_depth_levels = 5

    def __init__(
        self,
        *,
        instruments: Optional[InstrumentIndex] = None,
        on_ticks: Optional[Callable[[str, Dict[str, Dict[str, Any]], Optional[float]], None]] = None,
        on_tick: Any = None,
        on_status: Any = None,
    ) -> None:
        super().__init__(instruments=instruments, on_ticks=on_ticks or on_tick)
        self._depth_levels = 5

    def set_depth_levels(self, levels: int) -> int:
        # KiteTicker binary full mode streams 5-level depth
        self._depth_levels = 5
        return 5

    def status(self) -> Dict[str, Any]:
        st = super().status()
        st["feed_mode"] = "full"
        st["depth_levels"] = self._depth_levels
        st["max_depth_levels"] = self.max_depth_levels
        return st

    def _build_connection_params(self) -> Tuple[str, Dict[str, str]]:
        token = zerodha_auth.get_valid_token()
        api_key = zerodha_auth.api_key
        if not api_key:
            raise ValueError("Zerodha API key not configured.")
        qs = urllib.parse.urlencode({"api_key": api_key, "access_token": token})
        return f"wss://ws.kite.trade?{qs}", {"X-Kite-Version": "3"}

    def _resolve_keys(self, subs: List[Dict[str, str]]) -> Dict[int, Dict[str, str]]:
        out: Dict[int, Dict[str, str]] = {}
        for item in subs:
            sym = item["symbol"]
            exch = item.get("exchange") or "NSE"
            itype = item.get("instrument_type") or ""
            resolved = self._instruments.resolve_key(
                sym, broker_id="zerodha", exchange=exch, instrument_type=itype or None
            )
            if resolved and str(resolved).isdigit():
                out[int(resolved)] = {"symbol": sym, "exchange": exch, "instrument_type": itype or "EQUITY"}
        return out

    def _send_subscribe(self, ws: StdlibWSClient, keys: List[Any]) -> None:
        int_keys = [int(k) for k in keys]
        for i in range(0, len(int_keys), 200):
            batch = int_keys[i : i + 200]
            ws.send_text(json.dumps({"a": "subscribe", "v": batch}))
            ws.send_text(json.dumps({"a": "mode", "v": ["full", batch]}))

    def _send_unsubscribe(self, ws: StdlibWSClient, keys: List[Any]) -> None:
        int_keys = [int(k) for k in keys]
        for i in range(0, len(int_keys), 200):
            batch = int_keys[i : i + 200]
            ws.send_text(json.dumps({"a": "unsubscribe", "v": batch}))

    def _handle_frame(self, opcode: int, payload: bytes) -> Tuple[Dict[str, Dict[str, Any]], Optional[float]]:
        # 1-byte heartbeat frames are normal on KiteTicker
        if opcode != 0x2 or len(payload) < 4:
            return {}, None
        with self._lock:
            token_meta = dict(self._key_meta)
        quotes = decode_zerodha_binary_packets(payload, token_meta_map=token_meta)
        return quotes, None


class BrokerWebSocketHub:
    """
    Modular coordinator for persistent broker WebSocket clients (`upstox`, `zerodha`,
    plus any third-party broker registered via `BrokerRegistry.register_plugin` or
    `register_ws_client_factory`) supporting Level-5 and Level-30 market depth.
    """

    def __init__(
        self,
        *,
        instruments: Optional[InstrumentIndex] = None,
        on_ticks: Optional[Callable[[str, Dict[str, Dict[str, Any]], Optional[float]], None]] = None,
    ) -> None:
        self._instruments = instruments or instrument_index
        self._on_ticks = on_ticks
        self._clients: Dict[str, _BaseBrokerWSClient] = {
            "upstox": UpstoxWSFeedClient(instruments=self._instruments, on_ticks=on_ticks),
            "zerodha": ZerodhaWSFeedClient(instruments=self._instruments, on_ticks=on_ticks),
        }

    def register_client(self, broker_id: str, client: _BaseBrokerWSClient) -> None:
        bid = (broker_id or "").strip().lower()
        if bid and client is not None:
            self._clients[bid] = client

    def _resolve_client(self, broker_id: str) -> Optional[_BaseBrokerWSClient]:
        bid = (broker_id or "").strip().lower()
        if not bid:
            return None
        client = self._clients.get(bid)
        if client is not None:
            return client
        # Lazy plug-and-play discovery from BrokerRegistry so adding a broker requires zero edits here
        try:
            from broker.registry import BrokerRegistry

            factory = BrokerRegistry.get_ws_client_factory(bid)
            if callable(factory):
                client = factory(instruments=self._instruments, on_ticks=self._on_ticks)
            elif BrokerRegistry.has(bid):
                adapter = BrokerRegistry.get(bid)
                if getattr(getattr(adapter, "capabilities", None), "supports_websocket", False):
                    client = adapter.create_ws_client(on_tick=self._on_ticks)
            if client is not None:
                self._clients[bid] = client
                return client
        except Exception as exc:
            logger.debug("Dynamic WS client discovery for '%s' skipped: %s", bid, exc)
        return None

    def supports(self, broker_id: str) -> bool:
        return self._resolve_client(broker_id) is not None

    def set_depth_levels(self, broker_id: str, levels: int) -> int:
        client = self._resolve_client(broker_id)
        if client is not None and hasattr(client, "set_depth_levels"):
            return int(client.set_depth_levels(levels))
        return 0

    def get_cached_depth(self, broker_id: str, symbol: str, *, max_age_sec: float = 30.0) -> Optional[Dict[str, Any]]:
        sym = (symbol or "").strip().upper()
        quotes, _ = self.get_cached_quotes(broker_id, [sym], max_age_sec=max_age_sec)
        q = quotes.get(sym)
        if isinstance(q, dict) and isinstance(q.get("depth"), dict):
            return dict(q["depth"])
        return None

    def set_scope_subscriptions(self, broker_id: str, scope: str, subs: List[Dict[str, str]]) -> None:
        client = self._resolve_client(broker_id)
        if client is not None:
            client.set_scope_subscriptions(scope, subs)

    def release_scope(self, scope: str, broker_id: Optional[str] = None) -> None:
        if broker_id:
            client = self._clients.get(broker_id.strip().lower())
            if client is not None:
                client.release_scope(scope)
            return
        for client in self._clients.values():
            client.release_scope(scope)

    def stop_all(self) -> None:
        for client in self._clients.values():
            client.stop()

    def is_connected(self, broker_id: str) -> bool:
        client = self._clients.get((broker_id or "").strip().lower())
        return bool(client and client.is_connected())

    def get_cached_quotes(
        self,
        broker_id: str,
        symbols: Iterable[str],
        *,
        max_age_sec: float = 30.0,
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[float]]:
        client = self._resolve_client(broker_id)
        if client is None:
            return {}, None
        return client.get_cached_quotes(symbols, max_age_sec=max_age_sec)

    def status(self, broker_id: Optional[str] = None) -> Dict[str, Any]:
        if broker_id:
            client = self._resolve_client(broker_id)
            return client.status() if client else {"broker_id": broker_id, "connected": False, "transport": "http_poll"}
        return {bid: c.status() for bid, c in self._clients.items()}


__all__ = [
    "BrokerWebSocketHub",
    "StdlibWSClient",
    "UpstoxWSFeedClient",
    "WebSocketClosedError",
    "ZerodhaWSFeedClient",
    "decode_upstox_feed_response",
    "decode_zerodha_binary_packets",
]
