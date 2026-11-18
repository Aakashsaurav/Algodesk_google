"""
core/feed_manager.py
====================
Phase 4/Live Terminal Hot-Standby Market Feed, Dynamic Asset Subscription
Registry, and Real-Time Broadcaster.

Manages ordered market-data feed providers:
- Primary Feed: Follows the user's assigned Data Connection (`upstox` or `zerodha` or `yfinance`)
- Standby Feeds: Cross-broker standby + `yfinance` public fallback

Features:
- Dynamic subscribe / unsubscribe for any Equity, Index, or F&O contract.
- Full quote snapshot telemetry (LTP, Open, High, Low, Prev Close, Change %,
  Volume, OI, Best Bid/Ask, tick direction, latency_ms).
- Real-time listener pub/sub + lazy background streaming thread (active ONLY
  while browser WebSocket/SSE clients are connected; zero idle CPU/RAM).
- Automatic failover primary -> standby with SEBI SHA-256 audit logging.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from broker.registry import BrokerRegistry
from broker.upstox.auth import auth_manager as upstox_auth
from broker.zerodha.auth import zerodha_auth_manager as zerodha_auth
from config import IST_TZ, config
from core.audit import AuditTrail, audit_trail
from core.instruments import instrument_index

logger = logging.getLogger(__name__)

_DEFAULT_SUBSCRIPTIONS: List[Dict[str, str]] = [
    {"symbol": "NIFTY", "exchange": "NSE", "instrument_type": "INDEX", "instrument_key": ""},
    {"symbol": "BANKNIFTY", "exchange": "NSE", "instrument_type": "INDEX", "instrument_key": ""},
    {"symbol": "RELIANCE", "exchange": "NSE", "instrument_type": "EQUITY", "instrument_key": ""},
    {"symbol": "TCS", "exchange": "NSE", "instrument_type": "EQUITY", "instrument_key": ""},
    {"symbol": "INFY", "exchange": "NSE", "instrument_type": "EQUITY", "instrument_key": ""},
    {"symbol": "HDFCBANK", "exchange": "NSE", "instrument_type": "EQUITY", "instrument_key": ""},
]

_YAHOO_LIVE_MAP = {
    "NIFTY": "^NSEI",
    "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NIFTY BANK": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX",
    "INDIA VIX": "^INDIAVIX",
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "INFY": "INFY.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "SBIN": "SBIN.NS",
    "ITC": "ITC.NS",
    "LT": "LT.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "AXISBANK": "AXISBANK.NS",
    "WIPRO": "WIPRO.NS",
    "BAJFINANCE": "BAJFINANCE.NS",
    "MARUTI": "MARUTI.NS",
    "SUNPHARMA": "SUNPHARMA.NS",
    "TATAMOTORS": "TATAMOTORS.NS",
    "TATASTEEL": "TATASTEEL.NS",
}


def _is_fno_subscription(sym: str, exch: str, itype: str) -> bool:
    u_ex = (exch or "").strip().upper()
    u_it = (itype or "").strip().upper()
    u_sym = (sym or "").strip().upper()
    if u_ex in {"NFO", "BFO", "MCX", "NSE_FO", "BSE_FO", "MCX_FO"}:
        return True
    if u_it in {"FUT", "FUTSTK", "FUTIDX", "OPTSTK", "OPTIDX", "CE", "PE"}:
        return True
    if any(ch.isdigit() for ch in u_sym) and (u_sym.endswith("FUT") or u_sym.endswith("CE") or u_sym.endswith("PE")):
        return True
    return False


def _sub_key(symbol: str, exchange: str = "NSE") -> str:
    return f"{(exchange or 'NSE').strip().upper()}:{(symbol or '').strip().upper()}"


@dataclass
class FeedSlotHealth:
    feed_id: str
    display_name: str
    role: str  # "primary" | "standby"
    transport: str  # "websocket+rest" | "http_poll"
    status: str  # "CONNECTED" | "STANDBY" | "FAILOVER" | "DISCONNECTED"
    active: bool = False
    ticks_received: int = 0
    error_count: int = 0
    reconnect_count: int = 0
    latency_ms: Optional[float] = None
    last_tick_ist: Optional[str] = None
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fetch_one_yahoo_quote(sub: Dict[str, str], timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    sym = str(sub.get("symbol") or "").strip().upper()
    exch = str(sub.get("exchange") or "NSE").strip().upper()
    itype = str(sub.get("instrument_type") or "").strip().upper()
    now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")

    if _is_fno_subscription(sym, exch, itype):
        return {
            "symbol": sym,
            "exchange": exch,
            "instrument_type": itype or "F&O",
            "ltp": None,
            "prev_close": None,
            "open": None,
            "high": None,
            "low": None,
            "change": None,
            "change_pct": None,
            "volume": 0,
            "oi": 0,
            "bid": None,
            "ask": None,
            "source": "yfinance",
            "transport": "unsupported_fno",
            "note": "Indian F&O live feed requires Upstox or Zerodha Kite session.",
            "ts_ist": now_ist,
        }

    if sym in _YAHOO_LIVE_MAP:
        ticker = _YAHOO_LIVE_MAP[sym]
    elif any(ch in sym for ch in (".", "^", "=", "-")):
        ticker = sym
    elif exch == "BSE":
        ticker = f"{sym}.BO"
    elif exch in {"US", "NASDAQ", "NYSE"}:
        ticker = sym
    else:
        ticker = f"{sym}.NS"

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=5d&interval=1d"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 AlgoDesk/4.0", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        return None
    meta = results[0].get("meta") or {}
    quote_ind = ((results[0].get("indicators") or {}).get("quote") or [{}])[0]

    def _last_valid(arr: Any) -> Optional[float]:
        if not isinstance(arr, list):
            return None
        for val in reversed(arr):
            if val is not None:
                return float(val)
        return None

    price = meta.get("regularMarketPrice")
    if price is None:
        price = _last_valid(quote_ind.get("close"))
    if price is None:
        return None

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    closes = [float(c) for c in (quote_ind.get("close") or []) if c is not None]
    if prev_close is None and len(closes) >= 2:
        prev_close = closes[-2]

    ltp = round(float(price), 2)
    prev_val = round(float(prev_close), 2) if prev_close is not None else None
    chg = round(ltp - prev_val, 2) if prev_val is not None else None
    chg_pct = round(((ltp - prev_val) / prev_val) * 100.0, 2) if (prev_val and prev_val != 0) else 0.0

    day_open = meta.get("regularMarketOpen") or _last_valid(quote_ind.get("open"))
    day_high = meta.get("regularMarketDayHigh") or _last_valid(quote_ind.get("high"))
    day_low = meta.get("regularMarketDayLow") or _last_valid(quote_ind.get("low"))
    vol = meta.get("regularMarketVolume") or _last_valid(quote_ind.get("volume")) or 0

    return {
        "symbol": sym,
        "exchange": exch,
        "instrument_type": itype or "EQUITY",
        "ltp": ltp,
        "prev_close": prev_val,
        "open": round(float(day_open), 2) if day_open is not None else ltp,
        "high": round(float(day_high), 2) if day_high is not None else ltp,
        "low": round(float(day_low), 2) if day_low is not None else ltp,
        "change": chg,
        "change_pct": chg_pct,
        "volume": int(vol or 0),
        "oi": 0,
        "bid": None,
        "ask": None,
        "source": "yfinance",
        "transport": "live_chart_poll",
        "note": "",
        "ts_ist": now_ist,
    }


def _fetch_live_quotes_yfinance(symbols: List[Any], timeout: float = 4.0) -> Dict[str, Dict[str, Any]]:
    subs: List[Dict[str, str]] = []
    for item in symbols[:40]:
        if isinstance(item, dict):
            subs.append(item)
        else:
            subs.append({"symbol": str(item).strip().upper(), "exchange": "NSE", "instrument_type": ""})

    if not subs:
        return {}

    quotes: Dict[str, Dict[str, Any]] = {}
    max_workers = min(8, len(subs))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one_yahoo_quote, sub, timeout): sub for sub in subs}
        for fut, sub in futures.items():
            try:
                q = fut.result()
                if q:
                    quotes[q["symbol"]] = q
            except Exception as exc:
                sym = str(sub.get("symbol") or "").upper()
                exch = str(sub.get("exchange") or "NSE").upper()
                logger.debug("Yahoo quote error for %s (%s): %s", sym, exch, exc)
    return quotes


def _fetch_live_quotes_upstox(symbols: List[Any], timeout: float = 4.5) -> Dict[str, Dict[str, Any]]:
    token = upstox_auth.get_valid_token()
    key_map: Dict[str, Dict[str, str]] = {}
    for raw_item in symbols[:40]:
        if isinstance(raw_item, dict):
            sym = str(raw_item.get("symbol") or "").strip().upper()
            exch = str(raw_item.get("exchange") or "NSE").strip().upper()
            itype = str(raw_item.get("instrument_type") or "").strip().upper()
            ikey = str(raw_item.get("instrument_key") or "").strip()
        else:
            sym = str(raw_item).strip().upper()
            exch, itype, ikey = "NSE", "", ""

        resolved = (
            ikey
            if ikey and "|" in ikey
            else instrument_index.resolve_key(sym, broker_id="upstox", exchange=exch, instrument_type=itype or None)
        )
        if resolved:
            key_map[resolved] = {"symbol": sym, "exchange": exch, "instrument_type": itype}
    if not key_map:
        raise ValueError("No Upstox instrument keys resolved for subscribed symbols.")

    qs = urllib.parse.urlencode({"instrument_key": ",".join(key_map.keys())})
    url = f"{config.UPSTOX_BASE_URL}/v2/market-quote/quotes?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") or {}
    now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
    quotes: Dict[str, Dict[str, Any]] = {}
    for _, item in data.items():
        if not isinstance(item, dict):
            continue
        itoken = str(item.get("instrument_token") or "")
        meta = key_map.get(itoken) or {}
        sym = meta.get("symbol") or str(item.get("symbol") or "").upper()
        if not sym:
            continue
        ltp = round(float(item.get("last_price") or 0.0), 2)
        ohlc = item.get("ohlc") or {}
        prev_close = ohlc.get("close")
        net_chg = item.get("net_change")
        if net_chg is None and prev_close:
            net_chg = ltp - float(prev_close)
        chg_pct = (
            round((float(net_chg) / float(prev_close)) * 100.0, 2)
            if (net_chg is not None and prev_close and float(prev_close) != 0)
            else 0.0
        )
        depth = item.get("depth") or {}
        buys = depth.get("buy") or []
        sells = depth.get("sell") or []
        bid = round(float(buys[0].get("price")), 2) if buys and buys[0].get("price") else None
        ask = round(float(sells[0].get("price")), 2) if sells and sells[0].get("price") else None

        quotes[sym] = {
            "symbol": sym,
            "exchange": meta.get("exchange", "NSE"),
            "instrument_type": meta.get("instrument_type", "EQUITY"),
            "ltp": ltp,
            "prev_close": round(float(prev_close), 2) if prev_close is not None else None,
            "open": round(float(ohlc.get("open")), 2) if ohlc.get("open") is not None else ltp,
            "high": round(float(ohlc.get("high")), 2) if ohlc.get("high") is not None else ltp,
            "low": round(float(ohlc.get("low")), 2) if ohlc.get("low") is not None else ltp,
            "change": round(float(net_chg), 2) if net_chg is not None else None,
            "change_pct": chg_pct,
            "volume": int(item.get("volume") or 0),
            "oi": int(item.get("oi") or 0),
            "bid": bid,
            "ask": ask,
            "source": "upstox",
            "transport": "broker_ws+quote",
            "note": "",
            "ts_ist": now_ist,
        }
    return quotes


def _fetch_live_quotes_zerodha(symbols: List[Any], timeout: float = 4.5) -> Dict[str, Dict[str, Any]]:
    token = zerodha_auth.get_valid_token()
    api_key = zerodha_auth.api_key
    if not api_key:
        raise ValueError("Zerodha API key not configured.")
    pairs = []
    meta_by_key: Dict[str, Dict[str, str]] = {}
    for raw_item in symbols[:40]:
        if isinstance(raw_item, dict):
            sym = str(raw_item.get("symbol") or "").strip().upper()
            exch = str(raw_item.get("exchange") or "NSE").strip().upper()
            itype = str(raw_item.get("instrument_type") or "").strip().upper()
        else:
            sym = str(raw_item).strip().upper()
            exch, itype = "NSE", ""

        if sym == "NIFTY" and exch == "NSE":
            kite_key = "NSE:NIFTY 50"
        elif sym == "BANKNIFTY" and exch == "NSE":
            kite_key = "NSE:NIFTY BANK"
        elif sym == "SENSEX":
            kite_key = "BSE:SENSEX"
        else:
            kite_key = f"{exch}:{sym}"
        pairs.append(("i", kite_key))
        meta_by_key[kite_key] = {"symbol": sym, "exchange": exch, "instrument_type": itype}

    url = f"{config.ZERODHA_BASE_URL}/quote?{urllib.parse.urlencode(pairs)}"
    req = urllib.request.Request(
        url,
        headers={
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") or {}
    now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
    quotes: Dict[str, Dict[str, Any]] = {}
    for k_name, item in data.items():
        if not isinstance(item, dict):
            continue
        meta = meta_by_key.get(k_name) or {}
        sym = meta.get("symbol") or k_name.split(":")[-1]
        ltp = round(float(item.get("last_price") or 0.0), 2)
        ohlc = item.get("ohlc") or {}
        prev_close = ohlc.get("close")
        net_chg = item.get("net_change")
        if net_chg is None and prev_close:
            net_chg = ltp - float(prev_close)
        chg_pct = (
            round((float(net_chg) / float(prev_close)) * 100.0, 2)
            if (net_chg is not None and prev_close and float(prev_close) != 0)
            else 0.0
        )
        depth = item.get("depth") or {}
        buys = depth.get("buy") or []
        sells = depth.get("sell") or []
        bid = round(float(buys[0].get("price")), 2) if buys and buys[0].get("price") else None
        ask = round(float(sells[0].get("price")), 2) if sells and sells[0].get("price") else None

        quotes[sym] = {
            "symbol": sym,
            "exchange": meta.get("exchange", "NSE"),
            "instrument_type": meta.get("instrument_type", "EQUITY"),
            "ltp": ltp,
            "prev_close": round(float(prev_close), 2) if prev_close is not None else None,
            "open": round(float(ohlc.get("open")), 2) if ohlc.get("open") is not None else ltp,
            "high": round(float(ohlc.get("high")), 2) if ohlc.get("high") is not None else ltp,
            "low": round(float(ohlc.get("low")), 2) if ohlc.get("low") is not None else ltp,
            "change": round(float(net_chg), 2) if net_chg is not None else None,
            "change_pct": chg_pct,
            "volume": int(item.get("volume") or 0),
            "oi": int(item.get("oi") or 0),
            "bid": bid,
            "ask": ask,
            "source": "zerodha",
            "transport": "kiteticker+quote",
            "note": "",
            "ts_ist": now_ist,
        }
    return quotes


_DEFAULT_FETCHERS: Dict[str, Callable[[List[Any]], Dict[str, Dict[str, Any]]]] = {
    "upstox": _fetch_live_quotes_upstox,
    "zerodha": _fetch_live_quotes_zerodha,
    "yfinance": _fetch_live_quotes_yfinance,
}


class HotStandbyFeedManager:
    """
    Hot-Standby Feed Controller & Live Watchlist Subscription Streamer.
    Supports runtime subscribe/unsubscribe of any asset (Equity/Index/F&O),
    pushes live tick snapshots to connected WebSocket/SSE clients, and audits
    every failover transition into SEBI hash-chained `AuditTrail`.
    """

    def __init__(
        self,
        *,
        primary_id: str = "upstox",
        audit: Optional[AuditTrail] = None,
        fetchers: Optional[Dict[str, Callable[[List[Any]], Dict[str, Dict[str, Any]]]]] = None,
        watchlist_file: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._audit = audit or audit_trail
        self._fetchers = fetchers or dict(_DEFAULT_FETCHERS)
        self._watchlist_file = watchlist_file or (config.DATA_DIR / "watchlist.json")
        self._primary_id = (primary_id or "upstox").strip().lower()
        self._active_feed_id = self._primary_id
        self._forced_failover = False

        self._subscriptions: List[Dict[str, str]] = self._load_subscriptions()
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._tick_seq = 0
        self._tick_history: Dict[str, List[Dict[str, Any]]] = {}
        self._tick_events: List[Dict[str, Any]] = []
        self._failover_count = 0
        self._last_failover_reason = ""
        self._slots: Dict[str, FeedSlotHealth] = {}
        self._rebuild_slots(self._primary_id)

        self._listeners: set[queue.Queue] = set()
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_stop = threading.Event()
        self._stream_interval_sec = 2.0

    def _load_subscriptions(self) -> List[Dict[str, str]]:
        try:
            if self._watchlist_file.exists():
                raw = json.loads(self._watchlist_file.read_text(encoding="utf-8"))
                if isinstance(raw, list) and raw:
                    cleaned = []
                    for item in raw:
                        if isinstance(item, dict) and item.get("symbol"):
                            cleaned.append(
                                {
                                    "symbol": str(item["symbol"]).strip().upper(),
                                    "exchange": str(item.get("exchange") or "NSE").strip().upper(),
                                    "instrument_type": str(item.get("instrument_type") or "").strip().upper(),
                                    "instrument_key": str(item.get("instrument_key") or "").strip(),
                                }
                            )
                    if cleaned:
                        return cleaned
        except Exception:
            pass
        return [dict(item) for item in _DEFAULT_SUBSCRIPTIONS]

    def _save_subscriptions_locked(self) -> None:
        try:
            self._watchlist_file.parent.mkdir(parents=True, exist_ok=True)
            self._watchlist_file.write_text(
                json.dumps(self._subscriptions, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _ordered_chain(self, primary_id: str) -> List[str]:
        p = (primary_id or "upstox").strip().lower()
        order = [p]
        for candidate in ("upstox", "zerodha", "yfinance"):
            if candidate not in order:
                adapter = BrokerRegistry.get(candidate)
                if adapter.stage == "active" and adapter.capabilities.supports_market_data:
                    order.append(candidate)
        return order

    def _rebuild_slots(self, primary_id: str) -> None:
        p = (primary_id or "upstox").strip().lower()
        chain = self._ordered_chain(p)
        new_slots: Dict[str, FeedSlotHealth] = {}
        for idx, fid in enumerate(chain):
            adapter = BrokerRegistry.get(fid)
            prev = self._slots.get(fid)
            is_primary = idx == 0
            transport = "websocket+rest" if adapter.capabilities.supports_websocket else "http_poll"
            new_slots[fid] = FeedSlotHealth(
                feed_id=fid,
                display_name=adapter.display_name,
                role="primary" if is_primary else "standby",
                transport=transport,
                status="CONNECTED" if fid == self._active_feed_id else "STANDBY",
                active=(fid == self._active_feed_id),
                ticks_received=prev.ticks_received if prev else 0,
                error_count=prev.error_count if prev else 0,
                reconnect_count=prev.reconnect_count if prev else 0,
                latency_ms=prev.latency_ms if prev else None,
                last_tick_ist=prev.last_tick_ist if prev else None,
                last_error=prev.last_error if prev else "",
            )
        self._slots = new_slots

    def sync_primary(self, data_connection_id: str) -> None:
        clean = (data_connection_id or "upstox").strip().lower()
        with self._lock:
            if clean != self._primary_id:
                self._primary_id = clean
                if not self._forced_failover:
                    self._active_feed_id = clean
                self._rebuild_slots(clean)

    def _mark_active(self, feed_id: str) -> None:
        self._active_feed_id = feed_id
        for fid, slot in self._slots.items():
            slot.active = fid == feed_id
            if fid == feed_id:
                slot.status = "CONNECTED"
            elif slot.status == "CONNECTED":
                slot.status = "STANDBY"

    # ── Subscription Management ──────────────────────────────────────────

    def list_subscriptions(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(s) for s in self._subscriptions]

    def subscribe(
        self,
        symbol: str,
        exchange: str = "NSE",
        instrument_type: str = "",
        instrument_key: str = "",
        fetch_immediately: bool = True,
    ) -> Dict[str, Any]:
        clean_sym = (symbol or "").strip().upper()
        if not clean_sym:
            raise ValueError("Asset symbol is required to subscribe.")
        clean_exch = (exchange or "NSE").strip().upper()
        clean_itype = (instrument_type or "").strip().upper()
        clean_ikey = (instrument_key or "").strip()

        # Enrich metadata from SQLite index if user only typed symbol
        if not clean_ikey or not clean_itype:
            hits = instrument_index.search(clean_sym, exchange=clean_exch, limit=5)
            exact = next(
                (h for h in hits if h["trading_symbol"].upper() == clean_sym),
                hits[0] if hits else None,
            )
            if exact:
                clean_exch = exact.get("exchange") or clean_exch
                if not clean_itype:
                    clean_itype = str(exact.get("instrument_type") or "").upper()
                if not clean_ikey:
                    clean_ikey = str(exact.get("instrument_key") or "")

        entry = {
            "symbol": clean_sym,
            "exchange": clean_exch,
            "instrument_type": clean_itype or ("F&O" if _is_fno_subscription(clean_sym, clean_exch, clean_itype) else "EQUITY"),
            "instrument_key": clean_ikey,
        }

        with self._lock:
            self._subscriptions = [
                s
                for s in self._subscriptions
                if not (s["symbol"] == clean_sym and s["exchange"] == clean_exch)
            ]
            self._subscriptions.insert(0, entry)
            self._save_subscriptions_locked()

        if fetch_immediately:
            res = self.poll_ticks()
        else:
            res = {"ok": True, "message": f"Subscribed to {clean_exch}:{clean_sym}.", **self.status()}
            self._broadcast(res)

        res["subscribed_asset"] = entry
        res["message"] = f"Subscribed to {clean_exch}:{clean_sym} on live feed."
        return res

    def unsubscribe(self, symbol: str, exchange: Optional[str] = None) -> Dict[str, Any]:
        clean_sym = (symbol or "").strip().upper()
        clean_exch = (exchange or "").strip().upper() if exchange else None
        if not clean_sym:
            raise ValueError("Asset symbol is required to unsubscribe.")

        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [
                s
                for s in self._subscriptions
                if not (s["symbol"] == clean_sym and (not clean_exch or s["exchange"] == clean_exch))
            ]
            removed = before - len(self._subscriptions)
            if clean_exch:
                self._quotes.pop(_sub_key(clean_sym, clean_exch), None)
                self._tick_history.pop(_sub_key(clean_sym, clean_exch), None)
            else:
                for k in [k for k, v in self._quotes.items() if v.get("symbol") == clean_sym]:
                    self._quotes.pop(k, None)
                    self._tick_history.pop(k, None)
            self._save_subscriptions_locked()

        payload = {
            "ok": True,
            "removed": removed,
            "message": f"Unsubscribed {clean_sym}." if removed else f"{clean_sym} was not in active watchlist.",
            **self.status(),
        }
        self._broadcast(payload)
        return payload

    # ── Pub/Sub Streaming for WebSocket / SSE Clients ────────────────────

    def register_listener(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=16)
        with self._lock:
            self._listeners.add(q)
        self.ensure_streaming()
        return q

    def unregister_listener(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.discard(q)
            no_listeners = len(self._listeners) == 0
        if no_listeners:
            self._stream_stop.set()

    def _broadcast(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                q.put_nowait({"type": "tick_snapshot", **payload})
            except Exception:
                pass

    def ensure_streaming(self) -> None:
        with self._lock:
            if self._stream_thread and self._stream_thread.is_alive():
                return
            self._stream_stop.clear()
            self._stream_thread = threading.Thread(
                target=self._stream_loop,
                name="algodesk-live-ws-broadcaster",
                daemon=True,
            )
            self._stream_thread.start()

    def _stream_loop(self) -> None:
        while not self._stream_stop.is_set():
            with self._lock:
                has_listeners = len(self._listeners) > 0
                has_subs = len(self._subscriptions) > 0
            if not has_listeners:
                break
            if has_subs:
                try:
                    self.poll_ticks()
                except Exception as exc:
                    logger.debug("Live stream poll tick error: %s", exc)
            else:
                self._broadcast(self.status())
            self._stream_stop.wait(self._stream_interval_sec)

    # ── Polling & Failover Engine ────────────────────────────────────────

    def poll_ticks(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        with self._lock:
            if symbols:
                self._subscriptions = [
                    {
                        "symbol": s.strip().upper(),
                        "exchange": "NSE",
                        "instrument_type": "",
                        "instrument_key": "",
                    }
                    for s in symbols
                    if s.strip()
                ][:40]
                self._save_subscriptions_locked()
            target_subs = [dict(s) for s in self._subscriptions]
            chain = self._ordered_chain(self._primary_id)
            if self._forced_failover and len(chain) > 1:
                candidates = [c for c in chain if c != self._primary_id]
            else:
                candidates = list(chain)

        if not target_subs:
            res = {"ok": True, "message": "No assets subscribed.", **self.status()}
            self._broadcast(res)
            return res

        prev_active = self._active_feed_id
        selected_feed: Optional[str] = None
        last_err = ""

        for fid in candidates:
            fetcher = self._fetchers.get(fid)
            if fetcher is None:
                continue
            started = time.perf_counter()
            try:
                is_builtin = fetcher in _DEFAULT_FETCHERS.values()
                fetcher_arg = target_subs if is_builtin else [s["symbol"] for s in target_subs]
                raw_quotes = fetcher(fetcher_arg)
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                if not raw_quotes:
                    raise RuntimeError(f"{fid} returned empty quote snapshot")
                now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
                with self._lock:
                    slot = self._slots[fid]
                    slot.latency_ms = elapsed_ms
                    slot.ticks_received += len(raw_quotes)
                    slot.last_tick_ist = now_ist
                    slot.last_error = ""
                    self._tick_seq += 1

                    # Reconcile quotes against current subscriptions
                    for sym_key, q in raw_quotes.items():
                        q_sym = str(q.get("symbol") or sym_key).strip().upper()
                        matched_sub = next(
                            (s for s in self._subscriptions if s["symbol"] == q_sym),
                            None,
                        )
                        q_exch = (
                            matched_sub["exchange"]
                            if matched_sub
                            else str(q.get("exchange") or "NSE").strip().upper()
                        )
                        storage_key = _sub_key(q_sym, q_exch)
                        prev_q = self._quotes.get(storage_key)
                        new_ltp = q.get("ltp")
                        old_ltp = prev_q.get("ltp") if prev_q else None
                        if new_ltp is not None and old_ltp is not None:
                            direction = "up" if new_ltp > old_ltp else ("down" if new_ltp < old_ltp else "flat")
                        else:
                            direction = "flat"

                        enriched = {
                            "instrument_type": (matched_sub["instrument_type"] if matched_sub else "") or q.get("instrument_type", "EQUITY"),
                            "instrument_key": (matched_sub["instrument_key"] if matched_sub else "") or q.get("instrument_key", ""),
                            **q,
                            "symbol": q_sym,
                            "exchange": q_exch,
                            "direction": direction,
                            "tick_seq": self._tick_seq,
                        }
                        self._quotes[storage_key] = enriched

                        if new_ltp is not None:
                            hist = self._tick_history.setdefault(storage_key, [])
                            hist.append({"ts_ist": enriched["ts_ist"], "ltp": float(new_ltp)})
                            if len(hist) > 40:
                                del hist[: len(hist) - 40]

                            self._tick_events.insert(
                                0,
                                {
                                    "seq": self._tick_seq,
                                    "ts_ist": enriched["ts_ist"],
                                    "symbol": q_sym,
                                    "exchange": q_exch,
                                    "ltp": new_ltp,
                                    "change_pct": enriched.get("change_pct"),
                                    "volume": enriched.get("volume", 0),
                                    "oi": enriched.get("oi", 0),
                                    "direction": direction,
                                    "source": enriched.get("source", fid),
                                },
                            )

                    if len(self._tick_events) > 60:
                        del self._tick_events[60:]

                    self._mark_active(fid)
                    if fid != prev_active:
                        self._failover_count += 1
                        self._last_failover_reason = last_err or f"Switched active feed {prev_active} -> {fid}"
                        self._audit.log_failover(
                            from_feed=prev_active,
                            to_feed=fid,
                            reason=self._last_failover_reason,
                        )
                selected_feed = fid
                break
            except Exception as exc:
                last_err = str(exc)
                with self._lock:
                    slot = self._slots[fid]
                    slot.error_count += 1
                    slot.status = "FAILOVER" if fid == self._primary_id else "DISCONNECTED"
                    slot.last_error = last_err

        if selected_feed is None:
            res = {
                "ok": False,
                "message": f"All configured feeds failed ({last_err}).",
                **self.status(),
            }
            self._broadcast(res)
            return res

        res = {
            "ok": True,
            "message": (
                f"Failover active on {selected_feed.upper()} (primary {self._primary_id.upper()} unavailable: {last_err})."
                if selected_feed != self._primary_id
                else f"Live quotes updated from primary feed ({selected_feed.upper()})."
            ),
            **self.status(),
        }
        self._broadcast(res)
        return res

    def trigger_failover(self, reason: str = "Operator failover drill / primary drop") -> Dict[str, Any]:
        with self._lock:
            chain = self._ordered_chain(self._primary_id)
            standby = next((c for c in chain if c != self._primary_id), None)
            if not standby:
                raise ValueError("No standby data feed is available to fail over to.")
            prev_active = self._active_feed_id
            self._forced_failover = True
            self._failover_count += 1
            self._last_failover_reason = reason
            self._slots[self._primary_id].status = "FAILOVER"
            self._slots[self._primary_id].active = False
            self._slots[self._primary_id].last_error = reason
            self._mark_active(standby)
            self._audit.log_failover(from_feed=prev_active, to_feed=standby, reason=reason, actor="operator")

        res = self.poll_ticks()
        res["message"] = f"Hot-standby failover engaged: {self._primary_id.upper()} -> {self._active_feed_id.upper()}."
        return res

    def recover_primary(self) -> Dict[str, Any]:
        with self._lock:
            prev_active = self._active_feed_id
            self._forced_failover = False
            self._slots[self._primary_id].reconnect_count += 1
            self._mark_active(self._primary_id)
            if prev_active != self._primary_id:
                self._audit.log_failover(
                    from_feed=prev_active,
                    to_feed=self._primary_id,
                    reason="Primary data feed restored by operator",
                    actor="operator",
                )
        return self.poll_ticks()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            ordered = self._ordered_chain(self._primary_id)
            ordered_quotes: List[Dict[str, Any]] = []
            seen_keys = set()
            for sub in self._subscriptions:
                sk = _sub_key(sub["symbol"], sub["exchange"])
                seen_keys.add(sk)
                q = self._quotes.get(sk)
                if q:
                    ordered_quotes.append({**q, "history": list(self._tick_history.get(sk, []))})
                else:
                    ordered_quotes.append(
                        {
                            "symbol": sub["symbol"],
                            "exchange": sub["exchange"],
                            "instrument_type": sub.get("instrument_type", "EQUITY"),
                            "instrument_key": sub.get("instrument_key", ""),
                            "ltp": None,
                            "prev_close": None,
                            "open": None,
                            "high": None,
                            "low": None,
                            "change": None,
                            "change_pct": None,
                            "volume": 0,
                            "oi": 0,
                            "bid": None,
                            "ask": None,
                            "source": self._active_feed_id,
                            "transport": "pending",
                            "direction": "flat",
                            "tick_seq": self._tick_seq,
                            "note": "",
                            "ts_ist": None,
                            "history": [],
                        }
                    )

            return {
                "primary_feed": self._primary_id,
                "active_feed": self._active_feed_id,
                "in_failover": self._active_feed_id != self._primary_id,
                "forced_failover": self._forced_failover,
                "failover_count": self._failover_count,
                "last_failover_reason": self._last_failover_reason,
                "tick_seq": self._tick_seq,
                "watchlist": [s["symbol"] for s in self._subscriptions],
                "subscriptions": [dict(s) for s in self._subscriptions],
                "slots": [self._slots[fid].to_dict() for fid in ordered if fid in self._slots],
                "quotes": ordered_quotes,
                "tick_events": list(self._tick_events[:30]),
            }


feed_manager = HotStandbyFeedManager()

__all__ = ["FeedSlotHealth", "HotStandbyFeedManager", "feed_manager"]
