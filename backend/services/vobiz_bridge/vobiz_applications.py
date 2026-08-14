"""Vobiz Application management (answer/hangup webhook apps) — best-effort REST.

Used by scripts/setup_vobiz_incoming.py to register the /vobiz/incoming
webhook so inbound calls route to this server.
"""

from __future__ import annotations

import httpx

VOBIZ_API_BASE = "https://api.vobiz.ai/api/v1"


async def _request(method: str, url: str, auth_id: str, auth_token: str, json_body=None):
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, url, json=json_body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"Vobiz {method} {url} -> {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


async def list_applications(auth_id: str, auth_token: str) -> list:
    url = f"{VOBIZ_API_BASE}/Account/{auth_id}/Application/"
    data = await _request("GET", url, auth_id, auth_token)
    if isinstance(data, list):
        return data
    return data.get("applications", data.get("results", [])) if isinstance(data, dict) else []


async def create_application(
    auth_id: str,
    auth_token: str,
    friendly_name: str,
    voice_url: str,
    voice_method: str = "POST",
    hangup_url: str = "",
    hangup_method: str = "POST",
) -> dict:
    url = f"{VOBIZ_API_BASE}/Account/{auth_id}/Application/"
    body = {
        "friendly_name": friendly_name,
        "voice_url": voice_url,
        "voice_method": voice_method,
    }
    if hangup_url:
        body["hangup_url"] = hangup_url
        body["hangup_method"] = hangup_method
    return await _request("POST", url, auth_id, auth_token, json_body=body)


async def attach_number_to_application(auth_id: str, auth_token: str, app_id: str, number: str) -> dict:
    url = f"{VOBIZ_API_BASE}/Account/{auth_id}/Application/{app_id}/Numbers/"
    return await _request("POST", url, auth_id, auth_token, json_body={"number": number})
