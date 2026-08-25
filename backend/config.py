"""Central configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent
# Resolved once at import — use for frontend paths (avoid counting Path.parents per route file).
REPO_ROOT = _REPO_ROOT
FRONTEND_DIR = REPO_ROOT / "frontend"

# Local dev often keeps secrets in repo-root `.env` while running `uvicorn` from `backend/`.
# Repo fills defaults; `backend/.env` overrides when both define the same key.
load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_BACKEND_DIR / ".env", override=True)
load_dotenv(override=True)


def _b(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=False)
class Settings:
    """Runtime settings for Vernika AI voice agent."""

    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    server_url: str = os.getenv("SERVER_URL", "http://localhost:8000")

    # When false: no RAG append and no live keyword RAG on Vobiz.
    rag_enabled: bool = _b("RAG_ENABLED", True)
    fast_dialing: bool = _b("FAST_DIALING", True)
    rag_db_path: str = os.getenv("RAG_DB_PATH", str(_BACKEND_DIR / "data" / "rag.db"))
    rag_top_k: int = int(os.getenv("RAG_TOP_K", "4"))
    rag_max_context_chars: int = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "3600"))

    # Main persistence (PostgreSQL). Thread-local connections live in core.storage.
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/vernika"
    )

    # Call recordings
    call_recording_enabled: bool = _b("CALL_RECORDING_ENABLED", True)
    call_recording_dir: str = os.getenv(
        "CALL_RECORDING_DIR", str(_BACKEND_DIR / "data" / "call_recordings")
    )

    # Gemini API — Google AI Studio key (speech & text)
    gemini_api_key: str = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    # Post-call transcript analysis (SQLite / manual-call modal + campaign summaries).
    # Default: hosted Gemma 4 — 26B MoE instruct (REST generateContent). Override with GEMINI_CALL_ANALYSIS_MODEL.
    gemini_call_analysis_model: str = os.getenv(
        "GEMINI_CALL_ANALYSIS_MODEL", "gemini-2.5-flash"
    ).strip()
    # REST endpoint for post-call analysis (env-driven).
    gemini_endpoint: str = os.getenv(
        "GEMINI_ENDPOINT",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    ).strip()
    # Post-call analysis system prompt — must be set in .env (GEMINI_CALL_ANALYSIS_PROMPT).
    gemini_call_analysis_prompt: str = os.getenv("GEMINI_CALL_ANALYSIS_PROMPT", "").strip()
    # IANA zone for interpreting recalled times from transcripts ("5pm", "tomorrow 9am").
    transcript_callback_tz: str = (os.getenv("TRANSCRIPT_CALLBACK_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata")

    # Outbound campaign quiet hours (hard block). Default: no dialing 20:30–09:30 local TZ.
    campaign_quiet_hours_enabled: bool = _b("CAMPAIGN_QUIET_HOURS_ENABLED", True)
    campaign_quiet_start: str = (os.getenv("CAMPAIGN_QUIET_START", "19:30").strip() or "19:30")
    campaign_quiet_end: str = (os.getenv("CAMPAIGN_QUIET_END", "09:30").strip() or "09:30")

    # Per-role outbound calling windows (local TZ). Windowed roles ignore the global
    # quiet window and may only dial inside their own window.
    sales_1_call_window_start: str = (os.getenv("SALES_1_CALL_WINDOW_START", "11:00").strip() or "11:00")
    sales_1_call_window_end: str = (os.getenv("SALES_1_CALL_WINDOW_END", "17:00").strip() or "17:00")
    sales_2_call_window_start: str = (os.getenv("SALES_2_CALL_WINDOW_START", "09:30").strip() or "09:30")
    sales_2_call_window_end: str = (os.getenv("SALES_2_CALL_WINDOW_END", "18:30").strip() or "18:30")

    # Gemini Live API (native speech-to-speech for sub-800ms latency on phone calls)
    gemini_live_model: str = os.getenv("GEMINI_LIVE_MODEL", "models/gemini-3.1-flash-live-preview").strip()
    gemini_live_voice: str = os.getenv("GEMINI_LIVE_VOICE", "Leda").strip()
    gemini_live_voice_sales_1: str = os.getenv("GEMINI_LIVE_VOICE_SALES_1", "").strip()
    gemini_live_voice_sales_2: str = os.getenv("GEMINI_LIVE_VOICE_SALES_2", "").strip()
    gemini_opening_style_prompt_female: str = os.getenv(
        "GEMINI_OPENING_STYLE_PROMPT_FEMALE",
        "ENGLISH-FIRST, MULTILINGUAL, FEMALE, AHMEDABAD — read this opening greeting in warm, friendly, professional English with an Indian accent. Primary language is English. Also fluent in Gujarati and Hindi. Detect and mirror the caller's language naturally. Speak at a natural conversational pace. This is a service advisor from Ahmedabad."
    ).strip()
    gemini_opening_style_prompt_male: str = os.getenv(
        "GEMINI_OPENING_STYLE_PROMPT_MALE",
        "ENGLISH-FIRST, MULTILINGUAL, MALE, AHMEDABAD — read this opening greeting in warm, friendly, professional English with an Indian accent. Primary language is English. Also fluent in Gujarati and Hindi. Detect and mirror the caller's language naturally. Speak at a natural conversational pace. This is a service advisor from Ahmedabad."
    ).strip()
    # Language auto-detection: Gemini Live auto-detects language when not constrained.
    # Do NOT set a languageCode — let the model handle multilingual input natively.

    # WhatsApp Business number (for wa.me links in email etc.)
    whatsapp_business_number: str = os.getenv("WHATSAPP_BUSINESS_NUMBER", "918065480885").strip()

    # SMTP email (Gmail app password for auto-sending project details)
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_email: str = os.getenv("SMTP_EMAIL", "").strip()
    smtp_app_password: str = os.getenv("SMTP_APP_PASSWORD", "").strip()

    # When True: skip disk/primed PCM opener — Gemini Live speaks the greeting (same engine as the call).
    # When False: use greeting_{role}.pcm captured via Gemini Live as the scripted opener.
    # Default True because the TTS fallback has been removed.
    gemini_live_first_opening: bool = _b("GEMINI_LIVE_FIRST_OPENING", True)
    # Turn-taking / barge-in: HIGH sensitivity Activity Detection + optional tighter profile (default on).
    gemini_live_aggressive_activity_detection: bool = _b("GEMINI_LIVE_AGGRESSIVE_ACTIVITY_DETECTION", True)
    gemini_live_vad_prefix_padding_ms: int = int(os.getenv("GEMINI_LIVE_VAD_PREFIX_PADDING_MS", "30"))
    gemini_live_vad_silence_duration_ms: int = int(os.getenv("GEMINI_LIVE_VAD_SILENCE_DURATION_MS", "450"))
    gemini_live_vad_prefix_padding_ms_ultra: int = int(os.getenv("GEMINI_LIVE_VAD_PREFIX_PADDING_ULTRA_MS", "30"))
    gemini_live_vad_silence_duration_ms_ultra: int = int(os.getenv("GEMINI_LIVE_VAD_SILENCE_DURATION_ULTRA_MS", "400"))
    # Hybrid VAD: client-side energy gate sends audioStreamEnd after this many
    # ms of caller silence following detected speech, finalizing the turn
    # instantly instead of waiting for Gemini's server-side end-of-speech.
    gemini_live_hybrid_end_silence_ms: int = int(os.getenv("GEMINI_LIVE_HYBRID_END_SILENCE_MS", "300"))
    gemini_live_hybrid_energy_threshold: float = float(os.getenv("GEMINI_LIVE_HYBRID_ENERGY_THRESHOLD", "350"))
    # Configurable sensitivity levels.
    # Use abbreviated names: START_SENSITIVITY_HIGH, END_SENSITIVITY_HIGH, etc.
    # Do NOT use full enum names (START_OF_SPEECH_SENSITIVITY_*) — they cause 1007 errors.
    gemini_live_start_sensitivity: str = (
        os.getenv("GEMINI_LIVE_START_SENSITIVITY", "START_SENSITIVITY_HIGH").strip()
    )
    gemini_live_end_sensitivity: str = (
        os.getenv("GEMINI_LIVE_END_SENSITIVITY", "END_SENSITIVITY_HIGH").strip()
    )
    # Appended system text nudging concise turns + yield-on-overlap (phone calls).
    gemini_live_append_turn_instructions: bool = _b("GEMINI_LIVE_APPEND_TURN_INSTRUCTIONS", True)
    # Max characters for the assembled system instruction (prompt + RAG knowledge).
    # Raised so long role prompts (e.g. OpusHire) still get their KB appendix.
    max_system_prompt_chars: int = int(os.getenv("MAX_SYSTEM_PROMPT_CHARS", "10000"))
    # When no scripted PCM opening: brief gate before forwarding callee mic → Gemini (avoids chopping first model syllable).
    vobiz_gemini_live_forward_mute_seconds: float = float(
        os.getenv("VOBIZ_GEMINI_FORWARD_MUTE_SECONDS", "0.2")
    )

    # Playout jitter buffer safety margin in seconds (default: 0.40 = 400ms).
    # Higher values (e.g. 0.16–0.24) help prevent robotic stutters on high-jitter networks.
    vobiz_playout_prebuffer_seconds: float = float(
        os.getenv("VOBIZ_PLAYOUT_PREBUFFER_SECONDS", "0.40")
    )

    # Conversation logging
    conversation_log_enabled: bool = _b("CONVERSATION_LOG_ENABLED", True)
    conversation_log_dir: str = os.getenv(
        "CONVERSATION_LOG_DIR", str(_BACKEND_DIR / "data" / "conversation_logs")
    )

    # Optional outbound bed noise under voice — **off by default**. Set BACKGROUND_MUSIC_ENABLED=1
    # plus BACKGROUND_MUSIC_PATH / BACKGROUND_MUSIC_VOLUME to re-enable (see live_session mixer).
    background_music_enabled: bool = _b("BACKGROUND_MUSIC_ENABLED", False)
    background_music_path: str = os.getenv("BACKGROUND_MUSIC_PATH", "").strip()
    background_music_volume: float = float(os.getenv("BACKGROUND_MUSIC_VOLUME", "0"))

    # Vobiz Telephony — Global Fallback
    vobiz_auth_id: str = os.getenv("VOBIZ_AUTH_ID", "").strip()
    vobiz_auth_token: str = os.getenv("VOBIZ_AUTH_TOKEN", "").strip()
    vobiz_from_number: str = os.getenv("VOBIZ_FROM_NUMBER", "").strip()
    vobiz_public_base_url: str = os.getenv("VOBIZ_PUBLIC_BASE_URL", "").strip()
    # Origin for Vobiz <Stream> WebSocket only (may differ from callback URL).
    # Quick tunnels often accept POST /vobiz/answer but fail WebSocket upgrades from carrier POPs.
    vobiz_stream_public_base_url: str = os.getenv("VOBIZ_STREAM_PUBLIC_BASE_URL", "").strip().rstrip("/")

    # Vobiz Telephony — Maruti Suzuki Arena role (Pitchxai counselor)
    vobiz_maruti_auth_id: str = (
        os.getenv("VOBIZ_maruti_AUTH_ID", "").strip()
        or os.getenv("VOBIZ_REAL_ESTATE_AUTH_ID", "").strip()
    )
    vobiz_maruti_auth_token: str = (
        os.getenv("VOBIZ_maruti_AUTH_TOKEN", "").strip()
        or os.getenv("VOBIZ_REAL_ESTATE_AUTH_TOKEN", "").strip()
    )
    vobiz_maruti_from_number: str = (
        os.getenv("VOBIZ_maruti_FROM_NUMBER", "").strip()
        or os.getenv("VOBIZ_REAL_ESTATE_FROM_NUMBER", "").strip()
    )

    # Vobiz Telephony — Sales 1 role (2 phone numbers)
    vobiz_sales_1_auth_id: str = os.getenv("VOBIZ_SALES_1_AUTH_ID", "").strip()
    vobiz_sales_1_auth_token: str = os.getenv("VOBIZ_SALES_1_AUTH_TOKEN", "").strip()
    vobiz_sales_1_phone_1: str = os.getenv("VOBIZ_SALES_1_PHONE_1", "").strip()
    vobiz_sales_1_phone_2: str = os.getenv("VOBIZ_SALES_1_PHONE_2", "").strip()

    # Vobiz Telephony — Sales 2 role (2 phone numbers)
    vobiz_sales_2_auth_id: str = os.getenv("VOBIZ_SALES_2_AUTH_ID", "").strip()
    vobiz_sales_2_auth_token: str = os.getenv("VOBIZ_SALES_2_AUTH_TOKEN", "").strip()
    vobiz_sales_2_phone_3: str = os.getenv("VOBIZ_SALES_2_PHONE_3", "").strip()
    vobiz_sales_2_phone_4: str = os.getenv("VOBIZ_SALES_2_PHONE_4", "").strip()

    # Legacy alias (real_estate console role / old env names)
    vobiz_real_estate_auth_id: str = os.getenv("VOBIZ_REAL_ESTATE_AUTH_ID", "").strip()
    vobiz_real_estate_auth_token: str = os.getenv("VOBIZ_REAL_ESTATE_AUTH_TOKEN", "").strip()
    vobiz_real_estate_from_number: str = os.getenv("VOBIZ_REAL_ESTATE_FROM_NUMBER", "").strip()

    # Legacy multi-role fields (unused for maruti, kept for attribute safety)
    vobiz_buyers_auth_id: str = os.getenv("VOBIZ_BUYERS_AUTH_ID", "").strip()
    vobiz_buyers_auth_token: str = os.getenv("VOBIZ_BUYERS_AUTH_TOKEN", "").strip()
    vobiz_buyers_from_number: str = os.getenv("VOBIZ_BUYERS_FROM_NUMBER", "").strip()

    # Opening/greeting line for outbound calls
    vobiz_opening_line_default: str = os.getenv("VOBIZ_OPENING_LINE_DEFAULT", "").strip()

    # Dariaan — auto book discovery call + WhatsApp Meet link (vernikaai / Interested only)
    whatsapp_proxy_enabled: bool = _b("WHATSAPP_PROXY_ENABLED", False)
    whatsapp_proxy_url: str = os.getenv("WHATSAPP_PROXY_URL", "http://127.0.0.1:3001").strip()
    whatsapp_proxy_secret: str = os.getenv("WHATSAPP_PROXY_SECRET", "").strip()

    # Meta WhatsApp Cloud API — direct outbound messaging
    whatsapp_access_token: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    whatsapp_phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    whatsapp_verify_token: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    whatsapp_inbound_leads_enabled: bool = _b("WHATSAPP_INBOUND_LEADS_ENABLED", True)
    whatsapp_auto_dial_dariaan: bool = _b("WHATSAPP_AUTO_DIAL_DARIAAN", False)

    # Dariaan WhatsApp QR / wa.me link
    dariaan_whatsapp_number: str = os.getenv("DARIAAN_WHATSAPP_NUMBER", "").strip()
    dariaan_whatsapp_qr_message: str = os.getenv("DARIAAN_WHATSAPP_QR_MESSAGE", "").strip()

    # OpenWA — WhatsApp API Gateway (replaces old whatsapp-proxy sidecar)
    openwa_enabled: bool = _b("OPENWA_ENABLED", False)
    openwa_api_url: str = os.getenv("OPENWA_API_URL", "http://127.0.0.1:2785").strip()
    openwa_api_key: str = os.getenv("OPENWA_API_KEY", "").strip()
    openwa_session_id: str = os.getenv("OPENWA_SESSION_ID", "").strip()

    # Daily calling limit per phone number
    daily_call_limit_per_phone: int = int(os.getenv("DAILY_CALL_LIMIT_PER_PHONE", "1000"))


settings = Settings()


def server_url_to_ws(url: str, path: str = "/ws") -> str:
    """Turn https://host into wss://host/path for Vobiz stream."""
    u = url.rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://") :] + path
    if u.startswith("http://"):
        return "ws://" + u[len("http://") :] + path
    return u + path


def validate_critical_config() -> list[str]:
    """Return list of human-readable configuration problems (empty if OK)."""
    problems: list[str] = []
    if not settings.gemini_api_key:
        problems.append("GEMINI_API_KEY / GOOGLE_API_KEY is not set")
    vb = (
        settings.vobiz_auth_id
        and settings.vobiz_auth_token
        and settings.vobiz_from_number
    )
    if vb and not settings.vobiz_public_base_url:
        problems.append(
            "Vobiz is partially configured (auth/from set) but VOBIZ_PUBLIC_BASE_URL is empty — "
            "outbound calls cannot deliver answer XML or media WebSocket."
        )
    if vb and settings.vobiz_public_base_url and "proxy.runpod.net" in settings.vobiz_public_base_url:
        problems.append(
            "VOBIZ_PUBLIC_BASE_URL uses RunPod HTTP proxy, which may not work externally. "
            "Consider switching to a Cloudflare tunnel or direct domain."
        )
    ts = settings.vobiz_stream_public_base_url or ""
    pub = settings.vobiz_public_base_url or ""
    if vb and pub and ("trycloudflare.com" in pub or "trycloudflare.dev" in pub) and not ts:
        problems.append(
            "VOBIZ_PUBLIC_BASE_URL looks like a Cloudflare quick tunnel — media WebSockets often never "
            "reach your server (calls ring then drop). Set VOBIZ_STREAM_PUBLIC_BASE_URL to your VPS "
            "http(s) origin with port (e.g. http://YOUR_IP:8000) while keeping callbacks on the tunnel "
            "if needed, or switch fully to a stable domain."
        )
    return problems
