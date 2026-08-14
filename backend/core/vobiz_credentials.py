"""Resolve Vobiz auth + CLI per console role (env overrides stale DB for dedicated trunks)."""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from config import settings
from core.outbound_numbers import resolve_outbound_from_number
from core.state import normalize_console_role


def _maruti_env_configured() -> bool:
    return bool(
        (settings.vobiz_maruti_auth_id or "").strip()
        and (settings.vobiz_maruti_auth_token or "").strip()
        and (settings.vobiz_maruti_from_number or "").strip()
    )


def _sales_1_env_configured() -> bool:
    return bool(
        (settings.vobiz_sales_1_auth_id or "").strip()
        and (settings.vobiz_sales_1_auth_token or "").strip()
        and (settings.vobiz_sales_1_phone_1 or "").strip()
    )


def _sales_2_env_configured() -> bool:
    return bool(
        (settings.vobiz_sales_2_auth_id or "").strip()
        and (settings.vobiz_sales_2_auth_token or "").strip()
        and (settings.vobiz_sales_2_phone_3 or "").strip()
    )


def _buyers_env_configured() -> bool:
    return bool(
        (settings.vobiz_buyers_auth_id or "").strip()
        and (settings.vobiz_buyers_auth_token or "").strip()
        and (settings.vobiz_buyers_from_number or "").strip()
    )


def _real_estate_env_configured() -> bool:
    return bool(
        (settings.vobiz_real_estate_auth_id or "").strip()
        and (settings.vobiz_real_estate_auth_token or "").strip()
        and (settings.vobiz_real_estate_from_number or "").strip()
    )


def resolve_vobiz_credentials(
    role: str,
    vobiz_cfg: Optional[Mapping[str, object]] = None,
) -> Tuple[str, str, str, str]:
    """
    Return (auth_id, auth_token, from_number, public_url) for outbound dial.

    ``maruti``, ``buyers``, and ``realestate`` use dedicated env trunks when configured so campaigns
    and manual calls never fall back to the global seller DID by accident.
    """
    r = normalize_console_role(role)
    vc = dict(vobiz_cfg or {})

    public_url = (
        str(vc.get("public_url") or settings.vobiz_public_base_url or "")
        .strip()
        .rstrip("/")
    )

    if r in ("real_estate", "realestate") and _real_estate_env_configured():
        return (
            settings.vobiz_real_estate_auth_id.strip(),
            settings.vobiz_real_estate_auth_token.strip(),
            settings.vobiz_real_estate_from_number.strip(),
            public_url,
        )

    if r == "maruti" and _maruti_env_configured():
        return (
            settings.vobiz_maruti_auth_id.strip(),
            settings.vobiz_maruti_auth_token.strip(),
            settings.vobiz_maruti_from_number.strip(),
            public_url,
        )

    if r == "sales_1" and _sales_1_env_configured():
        return (
            settings.vobiz_sales_1_auth_id.strip(),
            settings.vobiz_sales_1_auth_token.strip(),
            settings.vobiz_sales_1_phone_1.strip(),
            public_url,
        )

    if r == "sales_2" and _sales_2_env_configured():
        return (
            settings.vobiz_sales_2_auth_id.strip(),
            settings.vobiz_sales_2_auth_token.strip(),
            settings.vobiz_sales_2_phone_3.strip(),
            public_url,
        )

    if r == "buyers" and _buyers_env_configured():
        return (
            settings.vobiz_buyers_auth_id.strip(),
            settings.vobiz_buyers_auth_token.strip(),
            settings.vobiz_buyers_from_number.strip(),
            public_url,
        )

    if r == "buyers":
        bid = (settings.vobiz_buyers_auth_id or "").strip()
        btok = (settings.vobiz_buyers_auth_token or "").strip()
        bfrm = (settings.vobiz_buyers_from_number or "").strip()
        if bid and btok:
            frm = bfrm or resolve_outbound_from_number(role, vc)
            return bid, btok, frm, public_url
        if bfrm:
            auth_id = str(vc.get("auth_id") or settings.vobiz_auth_id or "").strip()
            auth_token = str(vc.get("auth_token") or settings.vobiz_auth_token or "").strip()
            if auth_id and auth_token:
                return auth_id, auth_token, bfrm, public_url

    auth_id = str(vc.get("auth_id") or settings.vobiz_auth_id or "").strip()
    auth_token = str(vc.get("auth_token") or settings.vobiz_auth_token or "").strip()
    from_number = resolve_outbound_from_number(role, vc)
    return auth_id, auth_token, from_number, public_url
