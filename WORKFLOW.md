# Uday Auto Link — AI Calling Agent: Workflow

An end-to-end workflow reference for the AI voice-calling system (Gemini Live + Vobiz VoIP + WhatsApp + FastAPI + SQLite + web dashboard).

---

## 1. System Components

```
┌──────────────────────────────────────────────────────────────────┐
│                        OPERATOR CONSOLE (Web)                    │
│   /login → /console — stats, lead manifest, call detail modal,    │
│   campaign controls, prompt tuning, RAG docs                      │
└────────────────────────────┬─────────────────────────────────────┘
                             │ REST + SSE (events)
┌────────────────────────────▼─────────────────────────────────────┐
│                     FASTAPI BACKEND (port 8000)                  │
│  • api/routes/     REST endpoints (console, campaign, vobiz,     │
│                     whatsapp, web_voice, events, auth, cases...)  │
│  • core/worker.py  Campaign worker loop + async scheduler loop    │
│  • core/state.py   Per-role in-memory state                        │
│  • core/storage.py SQLite persistence (leads, callbacks, calls)   │
│  • core/rag.py     Local FTS5 RAG store (service-center docs)     │
│  • prompts/priya.py  Per-role system prompt + RAG source text     │
└───────┬──────────────┬───────────────┬───────────────┬───────────┘
        │              │               │               │
  ┌─────▼─────┐  ┌─────▼──────┐  ┌─────▼───────┐  ┌─────▼────────┐
  │ Vobiz     │  │ Gemini Live│  │ OpenWA /    │  │ Google SMTP  │
  │ Telephony │  │ (real-time │  │ WhatsApp    │  │ (auto email) │
  │ (VoIP)    │  │  speech)   │  │ Cloud API   │  │              │
  └───────────┘  └────────────┘  └─────────────┘  └──────────────┘
```

---

## 2. Call Lifecycle (Outbound — the core loop)

```
        API: /api/campaign/toggle  (start)
                    │
                    ▼
   ┌───────────────────────────────────────────────┐
   │ 1. WORKER LOOP (per role: maruti/sales_1/      │
   │    sales_2)                                    │
   └───────────────────────────────────────────────┘
                     │
                     ▼
   2. Check quiet hours (blocked 19:30–09:30 IST) ──► sleep if blocked
                     │
                     ▼
   3. Fetch next lead from role queue (status=pending)
                     │
     ┌───────────────┼────────────────┐
     ▼               ▼                ▼
  4a. DNC check   4b. Duplicate     4c. Rate/pacing
      (blocked #)     check             (24–28 calls/hr/line)
                     │
                     ▼
   5. Pick outbound number (alternates per role)
                     │
                     ▼
   6. Vobiz call → WebSocket stream to Gemini Live
                     │
                     ▼
   7. Conversation happens (Greeting → service advisor)
                     │
   ┌─────────────────┴──────────────────┐
   ▼                                    ▼
Call CONNECTED                      Call FAILED / No answer
   │                                    │
   ▼                                    ▼
8. Save audio recording            9. Mark lead failed/busy
   + transcript (JSONL .jsonl)         + error reason
                     │                    │
                     ▼                    ▼
10. TRANSCRIBE via Gemini 2.5 Flash   Auto-schedule retry
                     │               (cooldown, max 2 retries
                     ▼                == 3 attempts total)
11. ANALYZE transcript
    (disposition, rating, emotion,
    callback_reminder_epoch, actions)
                     │
     ┌───────────────┼───────────────────┐
     ▼               ▼                   ▼
12a. WhatsApp      12b. Email         12c. Callback
     auto-send        auto-send          scheduled?
     (if requested)   (if requested)       │
                                          ▼
                                   Create scheduled_callback →
                                   worker promotes to pending when due
                     │
                     ▼
13. Finalize lead status: completed / not_interested /
    busy / failed / callback_scheduled
                     │
                     ▼
14. Dashboard updates (SSE events push live stats)
```

---

## 3. Inbound Call Flow

```
Customer dials service-center number
            │
            ▼
Vobiz receives → POST /vobiz/answer → creates lead (incoming)
            │
            ▼
Greeting (recorded PCM or Gemini Live opener)
            │
            ▼
Gemini Live conversation (Priya prompt — Gujarati-first, mirrors language)
            │
            ▼
Call ends → transcript + analysis → lead saved
            │
            ▼
If customer asked for details → WhatsApp/email auto-sent
```

---

## 4. Post-Call Intelligence Workflow

```
audio → Gemini 2.5 Flash (transcribe, speaker diarization)
                │
                ▼
        transcript text
                │
                ▼
Gemini analysis prompt → structured JSON:
   disposition (Interested / Not Interested / Busy /
                No Answer / Wrong Number / Call Later)
   → mapped to lead status via _disposition_to_status()
                │
                ▼
Parsed extractions:
   callback time ("tomorrow 9am") → epoch via zoneinfo (Asia/Kolkata)
   WhatsApp request flag
   Email request flag
   Virtual meet / site-visit flags
                │
                ▼
Auto-actions executed + dashboard updated
```

---

## 5. Failure & Retry Workflow

```
Lead fails (no answer / busy / wrong number / Vobiz error)
                │
                ▼
Error reason recorded on lead (`leads.error`)
                │
                ▼
Auto-retry scheduled (if no answer / busy):
  cooldown → re-dial → repeat
  Max 2 retries (3 total attempts)
  After limit → status = failed
                │
                ▼
WhatsApp details often sent anyway after failed call
(with consent) — "unified failed-call follow-up"
```

### Failure reasons to diagnose
| Error string | Root cause to check |
|---|---|
| `No answer / Timeout` | Caller didn't pick up — retry handles it |
| `Vobiz {status}: {message}` | Vobiz auth / number / webhook |
| `Telephony not configured` | Missing Vobiz creds for role (.env) |
| `Phone number is blocked (DNC)` | Number opted out — expected |
| `No phone number` | Lead CSV missing number |
| `Watchdog restart...` | Worker stalled, auto-restarted |

---

## 6. Multi-Role Isolation

```
role = maruti | sales_1 | sales_2
                      │
                      ▼
Per-role sandbox:
  • separate SQLite role data (leads filtered by role column)
  • separate campaign + outbound numbers
  • separate prompts / RAG / greeting (prompts/priya.py)
  • separate dashboard state
```

---

## 7. Development Workflow

```
1. EDIT CODE
   backend/       FastAPI + core logic + prompts
   frontend/      HTML/CSS/JS console
                 │
                 ▼
2. INSTALL
   pip install -r requirements.txt
                 │
                 ▼
3. CONFIG
   backend/.env   (GEMINI_API_KEY, VOBIZ_*, WHATSAPP_*, SMTP_*)
                 │
                 ▼
4. RUN (dev)
   uvicorn main:app --host 0.0.0.0 --port 8000   (from backend/)
   ─ OR docker:
   docker build -t vernika-bridge .
   docker run -p 8000:8000 vernika-bridge
                 │
                 ▼
5. VERIFY
   GET /health          → health check
   GET /console         → operator dashboard
   GET /docs            → OpenAPI / Swagger
                 │
                 ▼
6. DEPLOY (prod, per legacy config)
   VPS + systemd unit "data-edge"
   SERVER_URL / VOBIZ_PUBLIC_BASE_URL / VOBIZ_STREAM_PUBLIC_BASE_URL
   → must be reachable from Vobiz (WebSocket upgrade)
```

---

## 8. Runtime Checks after any change

| Check | Tool |
|---|---|
| Server alive | `GET /health` |
| Worker active | logs / dashboard campaign status |
| Calls dialing | dashboard campaign toggle + live leads |
| WhatsApp sending | `OPENWA_ENABLED=1` + OpenWA at `127.0.0.1:2785` |
| Quiet hours | `CAMPAIGN_QUIET_HOURS_ENABLED`, start/end IST |
| Retries | failed leads get `failed_call_retries` in `extra` |

---

## 9. Known Gaps in This Snapshot

- **`backend/services/` package is missing** — the call engine (vobiz_bridge, call_analyzer, transcriber, whatsapp_leads, callback_time, sandbox_manager, etc.) is imported but not present here. The real backend cannot boot until restored.
- Root `static/`, `templates/`, `login.html` are dead duplicates of `frontend/`.
- `app.py` is a **mock Flask sandbox** (port 7071) with fake data — used for demos only, not real calls.