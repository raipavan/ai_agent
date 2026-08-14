"""Meta WhatsApp Cloud API webhook + Dariaan QR / proxy pairing page."""

from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from loguru import logger

from config import settings
from services.whatsapp_leads import (
    parse_meta_webhook_messages,
    process_dariaan_whatsapp_inbound,
    send_whatsapp_project_details,
    send_whatsapp_text_message,
    wa_me_link,
)

router = APIRouter(tags=["whatsapp"])


@router.get("/api/whatsapp/webhook")
async def whatsapp_webhook_verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    """Meta webhook verification (subscribe in WhatsApp → Configuration)."""
    expected = (settings.whatsapp_verify_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="WHATSAPP_VERIFY_TOKEN not set on server — add it to .env first",
        )
    if hub_mode == "subscribe" and hub_verify_token == expected:
        logger.info("WhatsApp webhook verified")
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Invalid verify token")


@router.post("/api/whatsapp/webhook")
async def whatsapp_webhook_events(request: Request):
    """Inbound Meta Cloud API messages → Dariaan lead list."""
    if not settings.whatsapp_inbound_leads_enabled:
        return {"status": "ignored", "reason": "WHATSAPP_INBOUND_LEADS_ENABLED=0"}

    try:
        body = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid json"}

    messages = parse_meta_webhook_messages(body)
    if not messages:
        return {"status": "ok", "processed": 0}

    results = []
    for msg in messages:
        try:
            result = await process_dariaan_whatsapp_inbound(
                from_phone=msg["from"],
                profile_name=msg.get("profile_name") or "",
                message_text=msg.get("text") or "",
                wa_message_id=msg.get("message_id") or "",
            )
            results.append(result)
        except Exception as e:
            logger.exception("WhatsApp lead ingest failed: {}", e)
            results.append({"error": str(e)})

    return {"status": "ok", "processed": len(results), "results": results}


@router.get("/dariaan/whatsapp/qr.png")
async def dariaan_whatsapp_qr_png():
    """Downloadable QR — proxy pairing or wa.me fallback."""
    from fastapi.responses import FileResponse, RedirectResponse
    from config import FRONTEND_DIR

    if settings.whatsapp_proxy_enabled:
        try:
            base = (settings.whatsapp_proxy_url or "http://127.0.0.1:3001").rstrip("/")
            async with httpx.AsyncClient(timeout=15.0) as client:
                st = await client.get(f"{base}/status")
                if st.status_code == 200 and st.json().get("authenticated"):
                    raise HTTPException(status_code=204, detail="Already linked")
                qr = await client.get(f"{base}/qr")
                if qr.status_code == 200:
                    from fastapi.responses import Response
                    return Response(
                        content=qr.content,
                        media_type="image/png",
                        headers={"Cache-Control": "no-store, max-age=0"},
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Proxy QR png fallback: {}", e)

    static_png = FRONTEND_DIR / "static" / "dariaan_whatsapp_qr.png"
    if static_png.is_file():
        return FileResponse(
            static_png,
            media_type="image/png",
            filename="dariaan_whatsapp_qr.png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    number = settings.dariaan_whatsapp_number.strip()
    prefill = settings.dariaan_whatsapp_qr_message.strip()
    link = wa_me_link(number, prefill)
    return RedirectResponse(
        url=f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={quote(link, safe='')}",
        status_code=302,
    )


def _proxy_pairing_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Dariaan — Link WhatsApp</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 440px; margin: 40px auto; padding: 24px; text-align: center; color: #1a1a1a; }
    h1 { font-size: 1.35rem; margin-bottom: 0.25rem; }
    .sub { color: #555; font-size: 0.95rem; margin-bottom: 1rem; line-height: 1.4; }
    .warn { background: #fff8e6; border: 1px solid #f0d78c; border-radius: 8px; padding: 10px; font-size: 12px; color: #664; margin-bottom: 1rem; text-align: left; }
    #status { font-size: 14px; margin: 12px 0; padding: 10px; border-radius: 8px; background: #f4f4f4; }
    #status.ok { background: #e8f8ee; color: #186a3b; }
    img { border: 1px solid #ddd; border-radius: 12px; max-width: 320px; width: 100%; }
    .steps { font-size: 12px; color: #666; text-align: left; margin-top: 1.5rem; line-height: 1.5; }
  </style>
</head>
<body>
  <h1>Dariaan — Link WhatsApp</h1>
  <p class="sub">Scan with your phone to connect WhatsApp. New messages → lead list → AI calls Ananya.</p>
  <div class="warn"><strong>Unofficial Web link.</strong> Use a spare number. Meta may ban automated personal WhatsApp. Inbound read + low outbound only.</div>
  <div id="status">Checking connection…</div>
  <img id="qr" alt="WhatsApp pairing QR" width="320" height="320"/>
  <div class="steps">
    <strong>Steps:</strong>
    <ol>
      <li>Open WhatsApp on your phone</li>
      <li>Menu → <strong>Linked devices</strong> → <strong>Link a device</strong></li>
      <li>Scan the QR above</li>
    </ol>
  </div>
  <script>
    const qrEl = document.getElementById('qr');
    const stEl = document.getElementById('status');
    function setStatus(text, ok) {
      stEl.textContent = text;
      stEl.className = ok ? 'ok' : '';
    }
    async function refresh() {
      try {
        const st = await fetch('/api/whatsapp/proxy/status');
        const data = await st.json();
        if (data.authenticated && data.connected) {
          setStatus('Connected: ' + (data.phone || data.pushname || 'WhatsApp linked'), true);
          qrEl.style.display = 'none';
          return;
        }
        setStatus(data.has_qr ? 'Scan QR with WhatsApp → Linked devices' : 'Starting sidecar… refresh in a few seconds');
        qrEl.style.display = '';
        qrEl.src = '/api/whatsapp/proxy/qr?t=' + Date.now();
      } catch (e) {
        setStatus('Sidecar not reachable — is whatsapp-proxy running on port 3001?');
        qrEl.style.display = 'none';
      }
    }
    refresh();
    setInterval(refresh, 4000);
  </script>
</body>
</html>"""


@router.get("/dariaan/whatsapp", response_class=HTMLResponse)
async def dariaan_whatsapp_qr_page():
    """Pairing page: proxy QR (whatsapp-web.js) or wa.me customer QR."""
    if settings.whatsapp_proxy_enabled:
        return HTMLResponse(content=_proxy_pairing_html(), headers={"Cache-Control": "no-store, max-age=0"})

    number = settings.dariaan_whatsapp_number.strip()
    prefill = settings.dariaan_whatsapp_qr_message.strip()
    link = wa_me_link(number, prefill)
    if not link:
        raise HTTPException(status_code=503, detail="DARIAAN_WHATSAPP_NUMBER not configured")

    digits_display = number if number.startswith("+") else f"+{number.lstrip('+')}"
    qr_img = f"https://api.qrserver.com/v1/create-qr-code/?size=320x320&data={quote(link, safe='')}"
    webhook_hint = (settings.vobiz_public_base_url or settings.server_url or "").rstrip("/")
    webhook_url = f"{webhook_hint}/api/whatsapp/webhook" if webhook_hint else "/api/whatsapp/webhook"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Dariaan — WhatsApp QR</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 420px; margin: 40px auto; padding: 24px; text-align: center; color: #1a1a1a; }}
    h1 {{ font-size: 1.35rem; margin-bottom: 0.25rem; }}
    .sub {{ color: #555; font-size: 0.95rem; margin-bottom: 1.5rem; }}
    img {{ border: 1px solid #ddd; border-radius: 12px; }}
    .num {{ font-size: 1.1rem; font-weight: 600; margin: 1rem 0; }}
    a.btn {{ display: inline-block; margin-top: 1rem; padding: 12px 20px; background: #25D366; color: #fff; text-decoration: none; border-radius: 8px; font-weight: 600; }}
    .hint {{ font-size: 0.75rem; color: #888; margin-top: 2rem; text-align: left; line-height: 1.4; }}
    code {{ font-size: 0.7rem; word-break: break-all; }}
  </style>
</head>
<body>
  <h1>Dariaan</h1>
  <p class="sub">Customer QR — scan to message on WhatsApp (enable WHATSAPP_PROXY_ENABLED=1 for account linking).</p>
  <img src="/static/dariaan_whatsapp_qr.png" width="320" height="320" alt="WhatsApp QR code"
       onerror="this.src='{qr_img}'"/>
  <p class="num">{digits_display}</p>
  <a class="btn" href="{link}" target="_blank" rel="noopener">Open WhatsApp</a>
  <p class="hint">
    <strong>Meta API webhook:</strong><br/>
    <code>{webhook_url}</code>
  </p>
</body>
</html>"""
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store, max-age=0"})


@router.post("/api/whatsapp/send-dummy")
async def whatsapp_send_dummy(payload: dict):
    """Send dummy Maruti Suzuki Arena project details to test phone for testing.

    Payload: {"phone": "+918065480885"} or {"phone": "918065480885"}
    """
    phone = (payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    result = await send_whatsapp_project_details(phone, "Test — Maruti Suzuki Arena Details")
    if not result.get("sent"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return {"status": "ok", "result": result}


@router.post("/api/whatsapp/send-details")
async def whatsapp_send_details(payload: dict):
    """Send project details to any phone number via WhatsApp Cloud API.

    Payload: {"phone": "+918065480885", "summary": "Brochure & Price Sheet (optional)"}
    """
    phone = (payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    summary = (payload.get("summary") or "Maruti Suzuki Arena — Service Details").strip()
    result = await send_whatsapp_project_details(phone, summary)
    if not result.get("sent"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return {"status": "ok", "result": result}


@router.post("/api/whatsapp/send-message")
async def whatsapp_send_message(payload: dict):
    """Send a custom text message to any phone number via WhatsApp Cloud API.

    Payload: {"phone": "+918065480885", "text": "Your message here"}
    """
    phone = (payload.get("phone") or "").strip()
    text = (payload.get("text") or "").strip()
    if not phone or not text:
        raise HTTPException(status_code=400, detail="phone and text are required")
    result = await send_whatsapp_text_message(phone, text)
    if not result.get("sent"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return {"status": "ok", "result": result}
