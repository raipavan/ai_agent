#!/usr/bin/env python3
"""One-time setup: create Vobiz Application for incoming calls + attach all numbers.

Usage:
    python scripts/setup_vobiz_incoming.py

Run this once after deploying the incoming call handler code.
It reads Vobiz credentials from the environment (same .env as the main app).
"""

import os
import sys
import asyncio

# Ensure backend/ is on sys.path so we can import config & app modules
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from config import settings
from core.outbound_numbers import build_phone_to_role_map
from services.vobiz_bridge.vobiz_applications import (
    create_application,
    list_applications,
    attach_number_to_application,
)


PUBLIC_BASE = (settings.vobiz_public_base_url or "").rstrip("/")
INCOMING_URL = f"{PUBLIC_BASE}/vobiz/incoming"
HANGUP_URL = f"{PUBLIC_BASE}/vobiz/hangup"

AUTH_ID = (settings.vobiz_auth_id or "").strip()
AUTH_TOKEN = (settings.vobiz_auth_token or "").strip()


async def main():
    if not AUTH_ID or not AUTH_TOKEN:
        print("ERROR: VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN must be set in .env")
        sys.exit(1)

    if not PUBLIC_BASE:
        print("ERROR: VOBIZ_PUBLIC_BASE_URL must be set in .env (e.g. http://187.127.177.149:8000)")
        sys.exit(1)

    print(f"Vobiz Account: {AUTH_ID}")
    print(f"Incoming URL:  {INCOMING_URL}")
    print(f"Hangup URL:    {HANGUP_URL}")
    print()

    # ── Step 1: Check for existing application ──
    print("Checking existing applications...")
    existing = await list_applications(AUTH_ID, AUTH_TOKEN)
    app = None
    for a in existing:
        friendly = a.get("app_name", "") or a.get("friendly_name", "")
        if "Maruti" in friendly and "Incoming" in friendly:
            app = a
            print(f"  Found existing application: {friendly} (id={a.get('app_id', '?')})")
            break

    if not app:
        # Create a new application
        print("Creating new Vobiz Application 'Uday Auto Link Incoming'...")
        result = await create_application(
            AUTH_ID,
            AUTH_TOKEN,
            friendly_name="Uday Auto Link Incoming",
            voice_url=INCOMING_URL,
            voice_method="POST",
            hangup_url=HANGUP_URL,
            hangup_method="POST",
        )
        print(f"  Created: {result}")
        app = result

    app_id = app.get("app_id") or app.get("id") or ""
    if not app_id:
        print("ERROR: Could not get app_id from application response")
        print(f"  Response: {app}")
        sys.exit(1)

    print(f"\nApplication ID: {app_id}")
    print()

    # ── Step 2: Attach all phone numbers ──
    phone_map = build_phone_to_role_map()
    print(f"Phone numbers to attach ({len(phone_map)}):")
    for digits, role in phone_map.items():
        print(f"  {digits} -> {role}")

    for digits, role in phone_map.items():
        # Vobiz expects full E.164 format for phone numbers when attaching
        full_number = f"+{digits}" if not digits.startswith("+") else digits
        try:
            result = await attach_number_to_application(AUTH_ID, AUTH_TOKEN, app_id, full_number)
            print(f"  ✓ Attached {full_number} ({role}): {result.get('status', 'OK')}")
        except Exception as e:
            print(f"  ✗ Failed to attach {full_number} ({role}): {e}")

    print()
    print("Done! Incoming calls to any attached number will now route to /vobiz/incoming.")


if __name__ == "__main__":
    asyncio.run(main())
