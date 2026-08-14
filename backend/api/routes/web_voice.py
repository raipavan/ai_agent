"""Browser-only voice test WebSockets (Gemini Live); not used for PSTN / Vobiz."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from services.browser_voice_bridge import handle_browser_voice_ws

router = APIRouter(tags=["voice"])


@router.websocket("/ws/web-demo")
async def websocket_web_demo(websocket: WebSocket) -> None:
    await handle_browser_voice_ws(websocket)


@router.websocket("/ws/voice-test")
async def websocket_voice_test(websocket: WebSocket) -> None:
    await handle_browser_voice_ws(websocket)
