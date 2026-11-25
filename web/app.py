"""
web/app.py
==========
FastAPI application for AlgoDesk's Local Broker & Data Connectivity Hub.
Designed to run cleanly on Apple Silicon (MacBook Air M2 8GB) with minimal
resident memory (~45 MB).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import html
import json
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from web.security import RateLimitError, security_manager
from web.service import hub_service
from web.spa import has_spa_bundle, render_spa_html, resolve_spa_asset

_SHUTDOWN_EVENT = threading.Event()


def request_shutdown() -> None:
    """Signal all open WebSocket and SSE streams to exit cleanly."""
    _SHUTDOWN_EVENT.set()


def reset_shutdown() -> None:
    """Clear the shutdown signal when starting a new server instance."""
    _SHUTDOWN_EVENT.clear()


def is_shutting_down() -> bool:
    return _SHUTDOWN_EVENT.is_set()

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
TERMINAL_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "terminal.html"


class SelectionRequest(BaseModel):
    trading_broker: str
    data_connection: str


class CredentialsRequest(BaseModel):
    api_key: str
    api_secret: str
    redirect_uri: Optional[str] = None


class ReconnectStartRequest(BaseModel):
    redirect_uri: Optional[str] = None


class CallbackExchangeRequest(BaseModel):
    code_or_url: str
    redirect_uri: Optional[str] = None


class InstrumentSyncRequest(BaseModel):
    broker_id: str = "upstox"


class HistoricalDownloadRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    timeframe: str = "1day"
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    provider: Optional[str] = None
    instrument_type: Optional[str] = None
    instrument_key: Optional[str] = None


class FeedPollRequest(BaseModel):
    symbols: Optional[List[str]] = None


class FeedFailoverRequest(BaseModel):
    reason: Optional[str] = None


class PreflightRequest(BaseModel):
    symbols: Optional[List[str]] = None
    exchange: str = "NSE"


class KillSwitchRequest(BaseModel):
    engaged: bool
    reason: Optional[str] = ""


class LiveSubscriptionRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    instrument_type: Optional[str] = None
    instrument_key: Optional[str] = None


class ScreenerRequest(BaseModel):
    symbols: Optional[List[str]] = None
    universe: str = "CUSTOM"
    basket_id: Optional[str] = None
    exchange: str = "NSE"
    timeframe: str = "1day"
    timeframes: Optional[List[str]] = None
    lookback_days: Optional[int] = None
    as_of_date: Optional[str] = None
    screen_id: Optional[str] = None
    preset: str = "all_metrics"
    rule_def: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None
    provider: Optional[str] = None
    backend: str = "auto"
    async_job: bool = False


class UniverseSyncRequest(BaseModel):
    universes: Optional[List[str]] = None


class LiveScreenerStartRequest(BaseModel):
    symbols: Optional[List[str]] = None
    basket_id: Optional[str] = None
    universe: str = "CUSTOM"
    exchange: str = "NSE"
    timeframe: str = "1day"
    screen_id: Optional[str] = None
    preset: str = "all_metrics"
    rule_def: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None
    provider: Optional[str] = None
    backend: str = "auto"
    poll_interval_sec: float = 2.0


class BacktestRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    strategy: str = "ema_crossover"
    params: Optional[Dict[str, Any]] = None
    timeframe: str = "1day"
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    initial_capital: float = 500000.0
    slippage_bps: float = 5.0
    fee_bps: float = 5.0
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 12.0
    provider: Optional[str] = None
    backend: str = "auto"
    instrument_type: Optional[str] = None
    instrument_key: Optional[str] = None
    async_job: bool = False


class RiskLimitsUpdateRequest(BaseModel):
    total_capital_inr: Optional[float] = None
    max_capital_per_trade_pct: Optional[float] = None
    max_daily_loss_inr: Optional[float] = None
    max_open_positions: Optional[int] = None
    max_gross_exposure_inr: Optional[float] = None
    auto_engage_kill_switch: Optional[bool] = None


def _render_callback_page(title: str, detail: str, broker_id: str, success: bool) -> str:
    safe_title = html.escape(title)
    safe_detail = html.escape(detail)
    js_broker = json.dumps(broker_id)
    script = ""
    if success:
        script = (
            "<script>"
            "try {"
            f"  if (window.opener) {{ window.opener.postMessage({{type: 'algodesk-oauth-captured', brokerId: {js_broker}}}, '*'); }}"
            "} catch (e) {}"
            "setTimeout(() => {"
            "  if (window.opener) { window.close(); }"
            "  else { window.location.href = '/'; }"
            "}, 900);"
            "</script>"
        )
    return (
        "<!doctype html><html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem;'>"
        f"<h2 style='margin:0 0 0.75rem'>{safe_title}</h2>"
        f"<p style='color:#94a3b8;margin:0 0 1rem'>{safe_detail}</p>"
        "<a href='/' style='color:#38bdf8'>Return to AlgoDesk</a>"
        f"{script}"
        "</body></html>"
    )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        reset_shutdown()
        hub_service.start_background_services()
        try:
            yield
        finally:
            hub_service.stop_background_services()
            request_shutdown()

    app = FastAPI(
        title="AlgoDesk Broker & Data Hub",
        description="Local broker connectivity status, reconnect flow, and trading/data routing.",
        version="1.0.0",
        lifespan=_lifespan,
    )

    def _ws_authorized(websocket: WebSocket) -> bool:
        if not security_manager.is_auth_enforced():
            return True
        auth_hdr = websocket.headers.get("authorization")
        if auth_hdr and security_manager.verify_bearer(auth_hdr):
            return True
        q_tok = websocket.query_params.get("token")
        return bool(q_tok and security_manager.verify_raw_token(q_tok))

    @app.middleware("http")
    async def _bearer_auth_middleware(request: Request, call_next):
        if security_manager.is_protected_request(request.method, request.url.path):
            auth_hdr = request.headers.get("authorization")
            ok = security_manager.verify_bearer(auth_hdr)
            if not ok and request.url.path == "/api/live/stream":
                q_tok = request.query_params.get("token")
                ok = bool(q_tok and security_manager.verify_raw_token(q_tok))
            if not ok:
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Operator authentication required. Sign in with your admin passcode or ALGODESK_API_TOKEN.",
                        "auth_required": True,
                    },
                )
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        body_html = render_spa_html("/")
        if body_html is None:
            raise HTTPException(status_code=500, detail="Frontend SPA bundle and fallback template are missing")
        return HTMLResponse(body_html, headers={"Cache-Control": "no-store"})

    @app.get("/terminal", response_class=HTMLResponse)
    @app.get("/terminal/", response_class=HTMLResponse)
    def terminal_page() -> HTMLResponse:
        body_html = render_spa_html("/terminal")
        if body_html is None:
            raise HTTPException(status_code=500, detail="Frontend SPA bundle and fallback template are missing")
        return HTMLResponse(body_html, headers={"Cache-Control": "no-store"})

    @app.get("/legacy", response_class=HTMLResponse)
    def legacy_page() -> HTMLResponse:
        if not TEMPLATE_PATH.exists():
            raise HTTPException(status_code=404, detail="Legacy template missing")
        return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def api_health() -> dict:
        return {
            "ok": True,
            "service": "algodesk",
            "phase": 6,
            "live_terminal": True,
            "analytics": True,
            "spa": has_spa_bundle(),
        }

    @app.get("/api/overview")
    def api_overview(refresh: bool = Query(default=True)) -> dict:
        return hub_service.get_overview(refresh=refresh)

    @app.post("/api/brokers/{broker_id}/probe")
    def api_probe_broker(broker_id: str) -> dict:
        try:
            return {"ok": True, "broker": hub_service.probe_one(broker_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/selection")
    def api_save_selection(payload: SelectionRequest) -> dict:
        try:
            return hub_service.save_selection(payload.trading_broker, payload.data_connection)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/brokers/{broker_id}/credentials")
    def api_save_credentials(broker_id: str, payload: CredentialsRequest) -> dict:
        try:
            return hub_service.save_credentials(
                broker_id=broker_id,
                api_key=payload.api_key,
                api_secret=payload.api_secret,
                redirect_uri=payload.redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/brokers/{broker_id}/reconnect")
    def api_start_reconnect(
        broker_id: str,
        request: Request,
        payload: Optional[ReconnectStartRequest] = None,
    ) -> dict:
        try:
            redirect_uri = payload.redirect_uri if payload else None
            return hub_service.start_reconnect(
                broker_id,
                redirect_uri=redirect_uri,
                server_port=request.url.port,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/brokers/{broker_id}/reconnect-status")
    def api_reconnect_status(broker_id: str) -> dict:
        return hub_service.get_reconnect_status(broker_id)

    @app.post("/api/brokers/{broker_id}/reconnect-cancel")
    def api_reconnect_cancel(broker_id: str) -> dict:
        return hub_service.cancel_reconnect(broker_id)

    @app.post("/api/brokers/{broker_id}/callback-exchange")
    def api_callback_exchange(broker_id: str, payload: CallbackExchangeRequest) -> dict:
        try:
            return hub_service.complete_reconnect(
                broker_id=broker_id,
                code_or_url=payload.code_or_url,
                redirect_uri=payload.redirect_uri,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/brokers/{broker_id}/disconnect")
    def api_disconnect(broker_id: str) -> dict:
        try:
            return hub_service.disconnect_broker(broker_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/auth/{broker_id}/callback", response_class=HTMLResponse)
    def auth_callback(
        broker_id: str,
        code: Optional[str] = None,
        request_token: Optional[str] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> HTMLResponse:
        if error or (status and status.lower() not in {"success", "ok"}):
            return HTMLResponse(
                _render_callback_page(
                    title="Authorization declined",
                    detail=error or f"Broker returned status={status}",
                    broker_id=broker_id,
                    success=False,
                ),
                status_code=400,
            )
        auth_token = request_token or code
        if not auth_token:
            return HTMLResponse(
                _render_callback_page(
                    title="Missing authorization token",
                    detail="No code or request_token query parameter was returned by the broker.",
                    broker_id=broker_id,
                    success=False,
                ),
                status_code=400,
            )
        try:
            result = hub_service.complete_reconnect(broker_id=broker_id, code_or_url=auth_token)
            state = result.get("broker", {}).get("state", "UNKNOWN")
            return HTMLResponse(
                _render_callback_page(
                    title=f"{broker_id.title()} connection updated: {state}",
                    detail="Session token captured automatically and saved securely. Returning to AlgoDesk...",
                    broker_id=broker_id,
                    success=True,
                )
            )
        except Exception as exc:
            return HTMLResponse(
                _render_callback_page(
                    title="Token exchange failed",
                    detail=str(exc),
                    broker_id=broker_id,
                    success=False,
                ),
                status_code=400,
            )

    @app.get("/api/instruments/stats")
    def api_instrument_stats() -> dict:
        return hub_service.instrument_stats()

    @app.get("/api/instruments/search")
    def api_instrument_search(
        q: str = Query(default=""),
        broker_id: Optional[str] = Query(default=None),
        exchange: Optional[str] = Query(default=None),
        instrument_type: Optional[str] = Query(default=None),
        expiry: Optional[str] = Query(default=None),
        strike: Optional[float] = Query(default=None),
        limit: int = Query(default=25),
    ) -> dict:
        return hub_service.search_instruments(
            query=q,
            broker_id=broker_id,
            exchange=exchange,
            instrument_type=instrument_type,
            expiry=expiry,
            strike=strike,
            limit=limit,
        )

    @app.get("/api/instruments/fno")
    def api_instrument_fno(
        underlying: str = Query(default=""),
        exchange: str = Query(default="NFO"),
        contract_type: str = Query(default="FO"),
        expiry: Optional[str] = Query(default=None),
        strike: Optional[float] = Query(default=None),
        broker_id: Optional[str] = Query(default=None),
        limit: int = Query(default=40),
    ) -> dict:
        return hub_service.list_fno_contracts(
            underlying=underlying,
            exchange=exchange,
            contract_type=contract_type,
            expiry=expiry,
            strike=strike,
            broker_id=broker_id,
            limit=limit,
        )

    @app.post("/api/instruments/sync")
    def api_instrument_sync(payload: InstrumentSyncRequest) -> dict:
        try:
            return hub_service.sync_instruments(payload.broker_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/data/catalog")
    def api_data_catalog() -> dict:
        return hub_service.list_historical_catalog()

    @app.post("/api/data/download")
    def api_data_download(payload: HistoricalDownloadRequest) -> dict:
        try:
            return hub_service.download_historical(
                symbol=payload.symbol,
                exchange=payload.exchange,
                timeframe=payload.timeframe,
                from_date=payload.from_date,
                to_date=payload.to_date,
                provider=payload.provider,
                instrument_type=payload.instrument_type,
                instrument_key=payload.instrument_key,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/data/records")
    def api_data_records(
        symbol: str = Query(...),
        exchange: str = Query(default="NSE"),
        timeframe: str = Query(default="1day"),
        from_date: Optional[str] = Query(default=None),
        to_date: Optional[str] = Query(default=None),
        view: str = Query(default="snapshot"),
        limit: Optional[int] = Query(default=None),
        offset: int = Query(default=0),
    ) -> dict:
        try:
            return hub_service.get_historical_records(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                from_date=from_date,
                to_date=to_date,
                view=view,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/data/export.csv")
    def api_data_export_csv(
        symbol: str = Query(...),
        exchange: str = Query(default="NSE"),
        timeframe: str = Query(default="1day"),
        from_date: Optional[str] = Query(default=None),
        to_date: Optional[str] = Query(default=None),
    ) -> Response:
        csv_text = hub_service.export_historical_csv(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
        )
        safe_sym = symbol.strip().upper() or "DATA"
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe_sym}_{timeframe}.csv"'},
        )

    @app.get("/api/live/status")
    def api_live_status() -> dict:
        return hub_service.get_live_status()

    @app.post("/api/live/feeds/poll")
    @app.post("/api/live/poll")
    def api_live_feeds_poll(payload: Optional[FeedPollRequest] = None) -> dict:
        return hub_service.poll_live_feeds(symbols=payload.symbols if payload else None)

    @app.post("/api/live/feeds/failover")
    @app.post("/api/live/failover")
    def api_live_feeds_failover(payload: Optional[FeedFailoverRequest] = None) -> dict:
        try:
            return hub_service.trigger_feed_failover(reason=payload.reason if payload else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/live/feeds/recover")
    @app.post("/api/live/recover-primary")
    def api_live_feeds_recover() -> dict:
        return hub_service.recover_primary_feed()

    @app.get("/api/live/subscriptions")
    def api_live_subscriptions() -> dict:
        return hub_service.get_subscriptions()

    @app.post("/api/live/subscribe")
    def api_live_subscribe(payload: LiveSubscriptionRequest) -> dict:
        try:
            return hub_service.subscribe_asset(
                symbol=payload.symbol,
                exchange=payload.exchange,
                instrument_type=payload.instrument_type,
                instrument_key=payload.instrument_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/live/unsubscribe")
    def api_live_unsubscribe(payload: LiveSubscriptionRequest) -> dict:
        try:
            return hub_service.unsubscribe_asset(
                symbol=payload.symbol,
                exchange=payload.exchange,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/live/stream")
    async def api_live_stream(request: Request) -> StreamingResponse:
        listener_q = hub_service.register_live_listener()

        async def _event_gen():
            try:
                initial = hub_service.get_live_status()["feeds"]
                yield f"data: {json.dumps({'type': 'tick_snapshot', **initial})}\n\n"
                while not is_shutting_down():
                    if await request.is_disconnected():
                        break
                    event = None
                    try:
                        event = listener_q.get_nowait()
                    except queue.Empty:
                        pass
                    if event is not None:
                        yield f"data: {json.dumps(event)}\n\n"
                    else:
                        await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                return
            finally:
                hub_service.unregister_live_listener(listener_q)

        return StreamingResponse(
            _event_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        listener_q = hub_service.register_live_listener()

        async def _sender() -> None:
            initial = hub_service.get_live_status()["feeds"]
            await websocket.send_text(json.dumps({"type": "tick_snapshot", **initial}))
            while not is_shutting_down():
                event = None
                try:
                    event = listener_q.get_nowait()
                except queue.Empty:
                    pass
                if event is not None:
                    await websocket.send_text(json.dumps(event))
                else:
                    await asyncio.sleep(0.2)

        async def _receiver() -> None:
            while not is_shutting_down():
                raw = await websocket.receive_text()
                try:
                    cmd = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    cmd = {}
                action = str(cmd.get("action") or "").strip().lower()
                if action == "subscribe":
                    try:
                        res = hub_service.subscribe_asset(
                            symbol=str(cmd.get("symbol") or ""),
                            exchange=str(cmd.get("exchange") or "NSE"),
                            instrument_type=cmd.get("instrument_type"),
                            instrument_key=cmd.get("instrument_key"),
                        )
                        await websocket.send_text(json.dumps({"type": "ack", **res}))
                    except Exception as exc:
                        await websocket.send_text(json.dumps({"type": "error", "detail": str(exc)}))
                elif action == "unsubscribe":
                    try:
                        res = hub_service.unsubscribe_asset(
                            symbol=str(cmd.get("symbol") or ""),
                            exchange=cmd.get("exchange"),
                        )
                        await websocket.send_text(json.dumps({"type": "ack", **res}))
                    except Exception as exc:
                        await websocket.send_text(json.dumps({"type": "error", "detail": str(exc)}))
                elif action == "poll":
                    res = hub_service.poll_live_feeds()
                    await websocket.send_text(json.dumps({"type": "tick_snapshot", **res}))

        sender_task = asyncio.create_task(_sender())
        receiver_task = asyncio.create_task(_receiver())
        try:
            await asyncio.wait({sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            for task in (sender_task, receiver_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender_task, receiver_task, return_exceptions=True)
            hub_service.unregister_live_listener(listener_q)
            if is_shutting_down():
                try:
                    await websocket.close(code=1001)
                except Exception:
                    pass

    @app.post("/api/live/preflight")
    def api_live_preflight(payload: Optional[PreflightRequest] = None) -> dict:
        return hub_service.run_preflight(
            symbols=payload.symbols if payload else None,
            exchange=payload.exchange if payload else "NSE",
        )

    @app.post("/api/live/kill-switch")
    def api_live_kill_switch(payload: KillSwitchRequest) -> dict:
        return hub_service.set_kill_switch(
            engaged=payload.engaged,
            reason=payload.reason or "",
        )

    @app.get("/api/audit/verify")
    def api_audit_verify(limit: int = Query(default=25)) -> dict:
        return hub_service.verify_audit_chain(limit=limit)

    @app.websocket("/ws/live-screener")
    async def ws_live_screener(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        listener_q = hub_service.register_live_screener_listener()

        async def _sender() -> None:
            initial = hub_service.get_live_screener_status()
            await websocket.send_text(json.dumps({"type": "live_screen:state", **initial}))
            while not is_shutting_down():
                event = None
                try:
                    event = listener_q.get_nowait()
                except queue.Empty:
                    pass
                if event is not None:
                    await websocket.send_text(json.dumps(event))
                else:
                    await asyncio.sleep(0.15)

        async def _receiver() -> None:
            while not is_shutting_down():
                raw = await websocket.receive_text()
                try:
                    cmd = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    cmd = {}
                action = str(cmd.get("action") or "").strip().lower()
                try:
                    if action == "start":
                        res = hub_service.start_live_screener(
                            symbols=cmd.get("symbols") if isinstance(cmd.get("symbols"), list) else None,
                            universe=str(cmd.get("universe", "CUSTOM")),
                            exchange=str(cmd.get("exchange", "NSE")),
                            timeframe=str(cmd.get("timeframe", "1day")),
                            screen_id=cmd.get("screen_id"),
                            preset=str(cmd.get("preset", "all_metrics")),
                            params=cmd.get("params") if isinstance(cmd.get("params"), dict) else None,
                            provider=cmd.get("provider"),
                            backend=str(cmd.get("backend", "auto")),
                            poll_interval_sec=float(cmd.get("poll_interval_sec", 2.0) or 2.0),
                        )
                        await websocket.send_text(json.dumps({"type": "live_screen:state", **res}))
                    elif action == "stop":
                        res = hub_service.stop_live_screener()
                        await websocket.send_text(json.dumps({"type": "live_screen:stopped", **res}))
                    elif action == "status":
                        res = hub_service.get_live_screener_status()
                        await websocket.send_text(json.dumps({"type": "live_screen:state", **res}))
                except Exception as exc:
                    await websocket.send_text(json.dumps({"type": "error", "detail": str(exc)}))

        sender_task = asyncio.create_task(_sender())
        receiver_task = asyncio.create_task(_receiver())
        try:
            await asyncio.wait({sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            for task in (sender_task, receiver_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender_task, receiver_task, return_exceptions=True)
            hub_service.unregister_live_screener_listener(listener_q)
            if is_shutting_down():
                try:
                    await websocket.close(code=1001)
                except Exception:
                    pass

    @app.get("/api/analytics/meta")
    def api_analytics_meta() -> dict:
        return hub_service.get_analytics_metadata()

    @app.get("/api/analytics/universes")
    def api_analytics_universes() -> dict:
        return hub_service.list_nse_universes()

    @app.post("/api/analytics/universes/sync")
    def api_analytics_universes_sync(payload: Optional[UniverseSyncRequest] = None) -> dict:
        return hub_service.sync_nse_universes(universe_ids=payload.universes if payload else None)

    @app.get("/api/analytics/live-screener/status")
    def api_analytics_live_screener_status() -> dict:
        return hub_service.get_live_screener_status()

    @app.post("/api/analytics/live-screener/start")
    def api_analytics_live_screener_start(payload: Optional[LiveScreenerStartRequest] = None) -> dict:
        req = payload or LiveScreenerStartRequest()
        try:
            return hub_service.start_live_screener(
                symbols=req.symbols,
                basket_id=req.basket_id,
                universe=req.universe,
                exchange=req.exchange,
                timeframe=req.timeframe,
                screen_id=req.screen_id,
                preset=req.preset,
                rule_def=req.rule_def,
                params=req.params,
                provider=req.provider,
                backend=req.backend,
                poll_interval_sec=req.poll_interval_sec,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/analytics/live-screener/stop")
    def api_analytics_live_screener_stop() -> dict:
        return hub_service.stop_live_screener()

    @app.get("/api/analytics/indicators/verify")
    def api_analytics_indicators_verify(
        symbol: Optional[str] = Query(default=None),
        exchange: str = Query(default="NSE"),
        timeframe: str = Query(default="1day"),
    ) -> dict:
        return hub_service.verify_indicators(symbol=symbol, exchange=exchange, timeframe=timeframe)

    @app.post("/api/analytics/reload")
    def api_analytics_reload() -> dict:
        return hub_service.reload_analytics_plugins()

    @app.post("/api/analytics/screener")
    def api_analytics_screener(payload: Optional[ScreenerRequest] = None, async_job: Optional[bool] = Query(default=None)) -> dict:
        req = payload or ScreenerRequest()
        is_async = req.async_job if async_job is None else async_job
        try:
            return hub_service.run_screener(
                symbols=req.symbols,
                basket_id=req.basket_id,
                universe=req.universe,
                exchange=req.exchange,
                timeframe=req.timeframe,
                timeframes=req.timeframes,
                lookback_days=req.lookback_days,
                as_of_date=req.as_of_date,
                screen_id=req.screen_id,
                preset=req.preset,
                rule_def=req.rule_def,
                params=req.params,
                provider=req.provider,
                backend=req.backend,
                async_job=is_async,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/analytics/backtest")
    def api_analytics_backtest(payload: BacktestRequest, async_job: Optional[bool] = Query(default=None)) -> dict:
        is_async = payload.async_job if async_job is None else async_job
        try:
            return hub_service.run_backtest(
                symbol=payload.symbol,
                exchange=payload.exchange,
                strategy=payload.strategy,
                params=payload.params,
                timeframe=payload.timeframe,
                from_date=payload.from_date,
                to_date=payload.to_date,
                initial_capital=payload.initial_capital,
                slippage_bps=payload.slippage_bps,
                fee_bps=payload.fee_bps,
                stop_loss_pct=payload.stop_loss_pct,
                take_profit_pct=payload.take_profit_pct,
                provider=payload.provider,
                backend=payload.backend,
                instrument_type=payload.instrument_type,
                instrument_key=payload.instrument_key,
                async_job=is_async,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/analytics/jobs")
    def api_analytics_jobs(limit: int = Query(default=15)) -> dict:
        return hub_service.list_analytics_jobs(limit=limit)

    @app.get("/api/analytics/jobs/{job_id}")
    def api_analytics_job(job_id: str) -> dict:
        try:
            return hub_service.get_analytics_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/analytics/jobs/{job_id}/cancel")
    def api_analytics_job_cancel(job_id: str) -> dict:
        try:
            return hub_service.cancel_analytics_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/analytics/risk")
    def api_analytics_risk() -> dict:
        return hub_service.get_risk_status()

    @app.post("/api/analytics/risk")
    def api_analytics_risk_update(payload: RiskLimitsUpdateRequest) -> dict:
        try:
            return hub_service.update_risk_limits(payload.model_dump(exclude_none=True))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Phase 6: Full Streak Platform Routes ──────────────────────────────

    @app.get("/api/streak/meta")
    def api_streak_meta() -> dict:
        return hub_service.get_streak_meta()

    @app.get("/api/streak/discover")
    def api_streak_discover(category: Optional[str] = Query(default=None), mode: Optional[str] = Query(default=None)) -> dict:
        return hub_service.list_discover(category=category, mode=mode)

    @app.get("/api/streak/rules")
    def api_streak_rules(kind: Optional[str] = Query(default=None)) -> dict:
        return hub_service.list_visual_rules(kind=kind)

    @app.post("/api/streak/rules")
    def api_streak_rules_save(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.save_visual_rule(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/streak/rules/{rule_id}")
    def api_streak_rules_delete(rule_id: str) -> dict:
        return hub_service.delete_visual_rule(rule_id)

    @app.post("/api/streak/rules/export-python")
    def api_streak_rules_export(payload: Dict[str, Any]) -> dict:
        try:
            rule_obj = payload.get("rule_def") if isinstance(payload.get("rule_def"), dict) else payload
            return hub_service.export_visual_rule_to_python(rule_obj, save_to_disk=bool(payload.get("save_to_disk", False)))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/streak/baskets")
    def api_streak_baskets() -> dict:
        return hub_service.list_baskets()

    @app.post("/api/streak/baskets")
    def api_streak_baskets_save(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.save_basket(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/streak/baskets/{basket_id}")
    def api_streak_baskets_delete(basket_id: str) -> dict:
        return hub_service.delete_basket(basket_id)

    @app.post("/api/streak/charges/preview")
    def api_streak_charges_preview(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.preview_charges(
                entry_price=float(payload.get("entry_price", 1000.0) or 1000.0),
                exit_price=float(payload.get("exit_price", 1025.0) or 1025.0),
                quantity=int(payload.get("quantity", 100) or 100),
                profile=str(payload.get("profile", "zerodha")),
                segment=str(payload.get("segment", "equity_intraday")),
                direction=str(payload.get("direction", "LONG")),
                overrides=payload.get("overrides") if isinstance(payload.get("overrides"), dict) else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/backtest")
    def api_streak_backtest(payload: Dict[str, Any], async_job: Optional[bool] = Query(default=None)) -> dict:
        is_async = bool(payload.get("async_job", False)) if async_job is None else async_job
        try:
            return hub_service.run_streak_backtest(
                mode=str(payload.get("mode", "portfolio")),
                symbols=payload.get("symbols") if isinstance(payload.get("symbols"), list) else None,
                basket_id=payload.get("basket_id"),
                universe=payload.get("universe"),
                exchange=str(payload.get("exchange", "NSE")),
                strategy=str(payload.get("strategy", "ema_crossover")),
                rule_def=payload.get("rule_def") if isinstance(payload.get("rule_def"), dict) else None,
                params=payload.get("params") if isinstance(payload.get("params"), dict) else None,
                timeframe=str(payload.get("timeframe", "1day")),
                from_date=payload.get("from_date"),
                to_date=payload.get("to_date"),
                start_time=payload.get("start_time"),
                end_time=payload.get("end_time"),
                holding_type=payload.get("holding_type"),
                strategy_cycle=int(payload["strategy_cycle"]) if payload.get("strategy_cycle") not in (None, "", "-") else None,
                side=payload.get("side"),
                initial_capital=float(payload.get("initial_capital", 500000.0) or 500000.0),
                allocation_mode=str(payload.get("allocation_mode", "shared_pool")),
                max_open_positions=int(payload.get("max_open_positions", 5) or 5),
                risk_per_trade_pct=float(payload.get("risk_per_trade_pct", 1.5) or 1.5),
                signal_ranking=str(payload.get("signal_ranking", "momentum")),
                stop_loss_pct=3.0 if payload.get("stop_loss_pct") in (None, "") else float(payload.get("stop_loss_pct")),
                take_profit_pct=7.0 if payload.get("take_profit_pct") in (None, "") else float(payload.get("take_profit_pct")),
                trailing_sl_pct=0.0 if payload.get("trailing_sl_pct") in (None, "") else float(payload.get("trailing_sl_pct")),
                charges_profile=str(payload.get("charges_profile", "zerodha")),
                segment=str(payload.get("segment", "equity_intraday")),
                charges_overrides=payload.get("charges_overrides") if isinstance(payload.get("charges_overrides"), dict) else None,
                slippage_bps=5.0 if payload.get("slippage_bps") in (None, "") else float(payload.get("slippage_bps")),
                provider=payload.get("provider"),
                backend=str(payload.get("backend", "auto")),
                async_job=is_async,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/streak/backtest/runs")
    def api_streak_backtest_runs(limit: int = Query(default=20)) -> dict:
        return hub_service.list_backtest_runs(limit=limit)

    @app.get("/api/streak/backtest/{run_id}/ohlcv.csv")
    def api_streak_backtest_ohlcv_csv(run_id: str, symbol: Optional[str] = Query(default=None)) -> Response:
        try:
            filename, csv_body = hub_service.export_backtest_ohlcv_csv(run_id=run_id, symbol=symbol)
            return Response(
                content=csv_body.encode("utf-8"),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/formula/validate")
    def api_streak_formula_validate(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.validate_formula_expr(
                str(payload.get("expression") or payload.get("expr") or ""),
                expect_boolean=bool(payload.get("expect_boolean", False)),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/technicals")
    def api_streak_technicals(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.analyze_technicals(
                symbol=str(payload.get("symbol", "RELIANCE")),
                exchange=str(payload.get("exchange", "NSE")),
                timeframe=str(payload.get("timeframe", "1day")),
                provider=payload.get("provider"),
                backend=str(payload.get("backend", "auto")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/options/template")
    def api_streak_options_template(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.build_options_template(
                template_id=str(payload.get("template_id", "iron_condor")),
                spot=float(payload.get("spot", 24000.0) or 24000.0),
                step=float(payload.get("step", 100.0) or 100.0),
                lot_size=int(payload.get("lot_size", 75) or 75),
                dte_days=float(payload.get("dte_days", 7.0) or 7.0),
                iv_pct=float(payload.get("iv_pct", 15.0) or 15.0),
                charges_profile=str(payload.get("charges_profile", "zerodha")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/options/analyze")
    def api_streak_options_analyze(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.analyze_options(
                spot=float(payload.get("spot", 24000.0) or 24000.0),
                legs=payload.get("legs") if isinstance(payload.get("legs"), list) else [],
                dte_days=float(payload.get("dte_days", 7.0) or 7.0),
                risk_free_rate=float(payload.get("risk_free_rate", 0.068) or 0.068),
                charges_profile=str(payload.get("charges_profile", "zerodha")),
                charges_overrides=payload.get("charges_overrides") if isinstance(payload.get("charges_overrides"), dict) else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/streak/deployments")
    def api_streak_deployments() -> dict:
        return hub_service.list_deployments()

    @app.post("/api/streak/deployments/start")
    def api_streak_deployments_start(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.start_deployment(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/deployments/{deployment_id}/action")
    def api_streak_deployments_action(deployment_id: str, payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.control_deployment(deployment_id, str(payload.get("action", "")))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/streak/deployments/{deployment_id}")
    def api_streak_deployments_delete(deployment_id: str) -> dict:
        try:
            return hub_service.control_deployment(deployment_id, "delete")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/deployments/alerts/{alert_id}/action")
    def api_streak_deployments_alert_action(alert_id: str, payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.resolve_deployment_alert(alert_id, str(payload.get("action", "")))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/streak/plugins/source")
    def api_streak_plugin_source(kind: str = Query(default="screen"), id: str = Query(default="")) -> dict:
        try:
            return hub_service.get_python_plugin_source(kind=kind, plugin_id=id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/plugins/python")
    def api_streak_plugin_save(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.save_python_plugin(
                kind=str(payload.get("kind", "screen")),
                filename=str(payload.get("filename", "")),
                code=str(payload.get("code", "")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/streak/scanners/alerts")
    def api_streak_scanner_alerts(subscription_id: Optional[str] = Query(default=None)) -> dict:
        return hub_service.list_scanner_alerts(subscription_id=subscription_id)

    @app.post("/api/streak/scanners/alerts")
    def api_streak_scanner_alerts_save(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.save_scanner_alert(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/scanners/alerts/{alert_id}/action")
    def api_streak_scanner_alerts_action(alert_id: str, payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.control_scanner_alert(alert_id, str(payload.get("action", "")))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/streak/scanners/alerts/{alert_id}")
    def api_streak_scanner_alerts_delete(alert_id: str) -> dict:
        return hub_service.delete_scanner_alert(alert_id)

    @app.post("/api/streak/orders/quick")
    def api_streak_quick_order(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.place_quick_order(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/streak/deployments/reconcile")
    def api_streak_deployments_reconcile(payload: Optional[Dict[str, Any]] = None) -> dict:
        try:
            dep_id = payload.get("deployment_id") if isinstance(payload, dict) else None
            return hub_service.reconcile_deployments(deployment_id=dep_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/streak/oms/orders")
    def api_streak_oms_orders(
        deployment_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
    ) -> dict:
        return hub_service.list_oms_orders(deployment_id=deployment_id, limit=limit)

    @app.get("/api/data/history")
    def api_data_history(
        symbol: Optional[str] = Query(default=None),
        exchange: Optional[str] = Query(default=None),
        timeframe: Optional[str] = Query(default=None),
    ) -> dict:
        return hub_service.get_asset_data_history(symbol=symbol, exchange=exchange, timeframe=timeframe)

    @app.get("/api/security/status")
    def api_security_status() -> dict:
        return hub_service.get_security_status()

    @app.post("/api/security/claim")
    def api_security_claim(request: Request, payload: Dict[str, Any]) -> dict:
        client_ip = request.client.host if request.client else "local"
        try:
            return hub_service.claim_admin_passcode(
                str(payload.get("passcode", "")),
                bootstrap_code=str(payload.get("bootstrap_code") or "") or None,
                client_key=client_ip,
            )
        except RateLimitError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after_sec)},
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/security/login")
    def api_security_login(request: Request, payload: Dict[str, Any]) -> dict:
        client_ip = request.client.host if request.client else "local"
        try:
            return hub_service.login_admin(
                str(payload.get("passcode") or payload.get("token") or ""),
                client_key=client_ip,
            )
        except RateLimitError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after_sec)},
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/notifications/telegram")
    def api_telegram_get() -> dict:
        return hub_service.get_telegram_settings()

    @app.post("/api/notifications/telegram")
    def api_telegram_update(payload: Dict[str, Any]) -> dict:
        try:
            return hub_service.update_telegram_settings(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/notifications/telegram/test")
    def api_telegram_test() -> dict:
        try:
            return hub_service.send_telegram_test()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.websocket("/ws/deployments")
    async def ws_deployments(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        listener_q = hub_service.register_deployments_listener()

        async def _sender() -> None:
            initial = hub_service.list_deployments()
            await websocket.send_text(json.dumps({"type": "deployments:state", **initial}))
            while not is_shutting_down():
                event = None
                try:
                    event = listener_q.get_nowait()
                except queue.Empty:
                    pass
                if event is not None:
                    await websocket.send_text(json.dumps(event))
                else:
                    await asyncio.sleep(0.2)

        async def _receiver() -> None:
            while not is_shutting_down():
                await websocket.receive_text()

        sender_task = asyncio.create_task(_sender())
        receiver_task = asyncio.create_task(_receiver())
        try:
            await asyncio.wait({sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            for task in (sender_task, receiver_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender_task, receiver_task, return_exceptions=True)
            hub_service.unregister_deployments_listener(listener_q)
            if is_shutting_down():
                try:
                    await websocket.close(code=1001)
                except Exception:
                    pass

    @app.get("/{full_path:path}")
    def spa_static_or_fallback(full_path: str) -> Response:
        if full_path.startswith(("api/", "ws/", "auth/")):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path in {"favicon.ico", "apple-touch-icon.png", "apple-touch-icon-precomposed.png"}:
            return Response(status_code=204)
        asset = resolve_spa_asset(full_path)
        if asset is not None:
            raw_bytes, content_type, cache_control = asset
            return Response(
                content=raw_bytes,
                media_type=content_type,
                headers={"Cache-Control": cache_control},
            )
        if not full_path.startswith("assets/") and "." not in full_path.rsplit("/", 1)[-1]:
            body_html = render_spa_html("/" + full_path)
            if body_html is not None:
                return HTMLResponse(body_html, headers={"Cache-Control": "no-store"})
        raise HTTPException(status_code=404, detail="Not Found")

    return app


app = create_app()
