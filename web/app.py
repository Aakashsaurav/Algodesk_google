"""
web/app.py
==========
FastAPI application for AlgoDesk's Local Broker & Data Connectivity Hub.
Designed to run cleanly on Apple Silicon (MacBook Air M2 8GB) with minimal
resident memory (~45 MB).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
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
    def api_start_reconnect(broker_id: str, payload: Optional[ReconnectStartRequest] = None) -> dict:
        try:
            redirect_uri = payload.redirect_uri if payload else None
            return hub_service.start_reconnect(broker_id, redirect_uri=redirect_uri)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    def auth_callback(broker_id: str, code: Optional[str] = None, error: Optional[str] = None) -> HTMLResponse:
        if error:
            return HTMLResponse(
                f"<html><body style='font-family:sans-serif;background:#0F172A;color:#F8FAFC;padding:2rem'>"
                f"<h2>Authorization declined</h2><p>{error}</p>"
                f"<a href='/' style='color:#38BDF8'>Return to AlgoDesk</a></body></html>",
                status_code=400,
            )
        if not code:
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;background:#0F172A;color:#F8FAFC;padding:2rem'>"
                "<h2>Missing authorization code</h2>"
                "<a href='/' style='color:#38BDF8'>Return to AlgoDesk</a></body></html>",
                status_code=400,
            )
        try:
            result = hub_service.complete_reconnect(broker_id=broker_id, code_or_url=code)
            state = result.get("broker", {}).get("state", "UNKNOWN")
            return HTMLResponse(
                f"<html><body style='font-family:sans-serif;background:#0F172A;color:#F8FAFC;padding:2rem'>"
                f"<h2>{broker_id.title()} connection updated: {state}</h2>"
                f"<p>Session token saved securely. Redirecting back to AlgoDesk...</p>"
                f"<script>setTimeout(()=>window.location.href='/', 900);</script>"
                f"</body></html>"
            )
        except Exception as exc:
            return HTMLResponse(
                f"<html><body style='font-family:sans-serif;background:#0F172A;color:#F8FAFC;padding:2rem'>"
                f"<h2>Token exchange failed</h2><p>{exc}</p>"
                f"<a href='/' style='color:#38BDF8'>Return to AlgoDesk</a></body></html>",
                status_code=400,
            )

    return app


app = create_app()
