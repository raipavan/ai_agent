"""Browser voice websocket bridge (stub)."""

from __future__ import annotations


async def handle_browser_voice_ws(websocket):
    """Stub: accept then close the websocket."""
    try:
        await websocket.accept()
    except Exception:
        pass
    try:
        await websocket.close(code=1000)
    except Exception:
        pass
