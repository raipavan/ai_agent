"""Resolve outbound Vobiz ``from`` number per role without duplicating branching logic."""

from __future__ import annotations

import re
import time
from typing import Mapping, Optional

from config import settings
from core.state import normalize_console_role


# Numbers Vobiz rejected with 403 ("Number is blocked") are skipped for a
# cooldown so the dialer stops wasting attempts on a blocked line while the
# role's other line(s) still work. Keyed by digits; value = monotonic expiry.
_BLOCKED_UNTIL: dict[str, float] = {}
_BLOCK_COOLDOWN_SEC = 600.0


def _digits(s: object) -> str:
    """Keep only digits for CLI comparison."""

    return re.sub(r"\D", "", str(s or ""))


def mark_number_blocked(number: str, cooldown_sec: float | None = None) -> None:
    """Remember that Vobiz rejected this CLI (403). Skipped by the dialer briefly."""
    d = _digits(number)
    if not d:
        return
    _BLOCKED_UNTIL[d] = time.monotonic() + float(
        cooldown_sec if cooldown_sec is not None else _BLOCK_COOLDOWN_SEC
    )


def number_is_blocked(number: str) -> bool:
    d = _digits(number)
    expiry = _BLOCKED_UNTIL.get(d)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        _BLOCKED_UNTIL.pop(d, None)
        return False
    return True


def _filter_blocked(numbers: list[str]) -> list[str]:
    """Drop blocked lines, but only when at least one line remains usable."""
    allowed = [n for n in numbers if not number_is_blocked(n)]
    return allowed if allowed else numbers


def _cli_same_number(a: str, b: str) -> bool:
    """Cheap E164-ish equality using last ten digits when both long enough."""

    da = _digits(a)
    db = _digits(b)
    if len(da) >= 10 and len(db) >= 10:
        return da[-10:] == db[-10:]
    if da and db:
        return da == db
    return (str(a or "").strip() == str(b or "").strip())


def resolve_outbound_from_number(role: str, vobiz_cfg: Optional[Mapping[str, object]] = None) -> str:
    """Pick CLI: stored ``vobiz.from_number`` unless polluted; then per-role env; then global fallback."""

    vc = dict(vobiz_cfg or {})
    explicit = str(vc.get("from_number") or "").strip()

    r = normalize_console_role(role)

    fb_global = (settings.vobiz_from_number or "").strip()

    if explicit:
        return explicit

    if r == "maruti":
        per_role_raw = (
            settings.vobiz_maruti_from_number
            or settings.vobiz_real_estate_from_number
            or settings.vobiz_from_number
        )
    elif r == "sales_1":
        per_role_raw = settings.vobiz_sales_1_phone_1 or settings.vobiz_from_number
    elif r == "sales_2":
        per_role_raw = settings.vobiz_sales_2_phone_3 or settings.vobiz_from_number
    elif r == "buyers":
        per_role_raw = settings.vobiz_buyers_from_number
    else:
        per_role_raw = ""
    per_role = str(per_role_raw or "").strip()
    if per_role:
        return per_role
    return str(settings.vobiz_from_number or "").strip()


def get_all_outbound_numbers(role: str, vobiz_cfg: Optional[Mapping[str, object]] = None) -> list[str]:
    """Return all configured outbound phone numbers for a role (for round-robin dialing)."""
    vc = dict(vobiz_cfg or {})
    r = normalize_console_role(role)
    
    # Check if phone_numbers are stored in vobiz config
    stored_numbers = vc.get("phone_numbers", [])
    if stored_numbers:
        return _filter_blocked([n for n in stored_numbers if n])
    
    # Fallback to env vars (Define 5 concurrent sub-workers per role by repeating entries)
    if r == "sales_1":
        numbers = [
            settings.vobiz_sales_1_phone_1 or "",
            settings.vobiz_sales_1_phone_2 or "",
        ]
    elif r == "sales_2":
        numbers = [
            settings.vobiz_sales_2_phone_3 or "",
            settings.vobiz_sales_2_phone_4 or "",
        ]
    elif r == "maruti":
        numbers = [
            settings.vobiz_maruti_from_number
            or settings.vobiz_real_estate_from_number
            or settings.vobiz_from_number
            or "",
        ]
    else:
        numbers = [settings.vobiz_from_number or ""]
    
    return _filter_blocked([n for n in numbers if n])


# Simple round-robin counter per role
_rr_counters: dict[str, int] = {}


def get_next_outbound_number_for_role(role: str, vobiz_cfg: Optional[Mapping[str, object]] = None) -> str:
    """Get the next outbound phone number for a role using round-robin.
    Falls back to resolve_outbound_from_number if no multiple numbers configured."""
    numbers = get_all_outbound_numbers(role, vobiz_cfg)
    if not numbers:
        return resolve_outbound_from_number(role, vobiz_cfg)
    if len(numbers) == 1:
        return numbers[0]
    r = normalize_console_role(role)
    idx = _rr_counters.get(r, 0) % len(numbers)
    _rr_counters[r] = idx + 1
    return numbers[idx]


def build_phone_to_role_map() -> dict[str, str]:
    """Build reverse mapping: phone_digits -> role for incoming call routing."""
    mapping: dict[str, str] = {}
    roles_nums = {
        "sales_1": [
            settings.vobiz_sales_1_phone_1 or "",
            settings.vobiz_sales_1_phone_2 or "",
        ],
        "sales_2": [
            settings.vobiz_sales_2_phone_3 or "",
            settings.vobiz_sales_2_phone_4 or "",
        ],
    }
    for role, nums in roles_nums.items():
        for num in nums:
            digits_only = re.sub(r"\D", "", num)
            if digits_only:
                mapping[digits_only] = role
    # If no per-role numbers configured, fall back to global number -> sales_1
    if not mapping and settings.vobiz_from_number:
        global_digits = re.sub(r"\D", "", settings.vobiz_from_number)
        if global_digits:
            mapping[global_digits] = "sales_1"
    return mapping

