"""
web/app.py
==========
FastAPI application for AlgoDesk's Local Broker & Data Connectivity Hub.
Designed to run cleanly on Apple Silicon (MacBook Air M2 8GB) with minimal
resident memory (~45 MB).
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from web.service import hub_service

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"


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
    app = FastAPI(
        title="AlgoDesk Broker & Data Hub",
        description="Local broker connectivity status, reconnect flow, and trading/data routing.",
        version="1.0.0",
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        if not TEMPLATE_PATH.exists():
            raise HTTPException(status_code=500, detail="index.html template missing")
        return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))

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

    return app


app = create_app()
