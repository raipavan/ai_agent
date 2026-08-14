# Uday Auto Link — AI Calling Agent

An end-to-end AI-powered calling agent system for Uday Auto Link, Maruti Suzuki Arena Kathwada. Automated outbound calls via Gemini Live, WhatsApp auto-send of project details, intelligent callback scheduling, and a real-time dashboard with call transcripts, ratings, and emotion analysis.

## Features

- **Gemini Live AI Agent** — Real-time voice conversations using Gemini 3.1 Flash Live
- **Automated Campaign Dialing** — Upload CSV leads, schedule campaigns, dial sequentially
- **WhatsApp Auto-Send** — Automatically sends project details (brochure, pricing, location) via WhatsApp when customer requests
- **Callback Scheduling** — Agent asks for specific callback time, system auto-dials at scheduled time
- **Call Analysis** — Post-call Gemini analysis with rating, emotion, disposition, next steps
- **Audio Transcription** — Gemini 2.5 Flash transcribes calls with speaker diarization
- **Real-Time Dashboard** — Live campaign stats, lead manifest, call detail modals with transcripts and recordings
- **Multi-Role Isolation** — Sales 1 and Sales 2 have completely separate data, campaigns, and dashboards
- **Virtual Meet Scheduling** — Captures virtual walkthrough/demo requests as structured actions
- **Quiet Hours** — Respects calling hours (9:30 AM – 8:30 PM IST)

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Frontend (HTML/JS)                 │
│  Dashboard · Lead Manifest · Call Detail Modal       │
│  Role Toggle (Sales 1 / Sales 2)                    │
└──────────────────┬──────────────────────────────────┘
                   │ REST API
┌──────────────────▼──────────────────────────────────┐
│                FastAPI Backend                       │
│  Campaign Worker · Callback Executor · Analysis      │
│  WhatsApp Auto-Send · Gemini Live Bridge             │
└──────────────────┬──────────────────────────────────┘
                   │
    ┌──────────────┼──────────────┐
    │              │              │
┌───▼───┐   ┌─────▼─────┐  ┌────▼────┐
│Vobiz  │   │  Gemini   │  │OpenWA   │
│Telephony│  │  Live API │  │WhatsApp │
│(VoIP)  │  │           │  │Gateway  │
└────────┘   └───────────┘  └─────────┘
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.11+, FastAPI, SQLite |
| AI Agent | Gemini 3.1 Flash Live (real-time voice) |
| Analysis | Gemini 2.5 Flash (post-call QA) |
| Transcription | Gemini 2.5 Flash (audio → text) |
| Telephony | Vobiz Bridge (VoIP) |
| WhatsApp | OpenWA Gateway (port 2785) |
| Frontend | Vanilla JS, CSS Grid |

## Project Structure

```
Data-Edge/
├── backend/
│   ├── api/routes/          # FastAPI route handlers
│   ├── core/                # Business logic
│   │   ├── worker.py        # Campaign loop, callbacks, analysis
│   │   ├── storage.py       # SQLite database layer
│   │   └── campaign_payload.py
│   ├── prompts/             # Agent system prompts
│   │   ├── sales_1_prompt.txt
│   │   ├── sales_2_prompt.txt
│   │   └── maruti_prompt.txt
│   ├── services/
│   │   ├── analysis_prompt.py      # Gemini analysis prompt
│   │   ├── callback_time.py        # Callback epoch extraction
│   │   ├── call_analyzer.py        # Post-call analysis
│   │   ├── transcriber.py          # Audio transcription
│   │   ├── whatsapp_leads.py       # WhatsApp auto-send
│   │   └── vobiz_bridge/           # VoIP + Gemini Live
│   │       └── live_session.py     # WebSocket handler
│   └── data/                # SQLite DB + role data dirs
├── frontend/
│   ├── templates/           # HTML templates
│   └── static/js/           # Frontend JavaScript
│       ├── app.js           # Init, role switching
│       ├── campaign.js      # Manifest polling, state sync
│       ├── restored.js      # Lead table, call detail modal
│       └── api_utils.js     # API helpers
├── .env                     # Configuration (not committed)
└── requirements.txt
```

## Setup

### 1. Environment Variables (.env)

```bash
# Server
HOST=0.0.0.0
PORT=8000
SERVER_URL=https://your-domain.com

# Gemini
GEMINI_API_KEY=your_key
GEMINI_LIVE_MODEL=models/gemini-3.1-flash-live-preview
GEMINI_LIVE_VOICE=Fenrir
GEMINI_LIVE_LANGUAGE=en-IN

# Vobiz Telephony
VOBIZ_AUTH_ID=your_auth_id
VOBIZ_AUTH_TOKEN=your_token
VOBIZ_FROM_NUMBER=+91XXXXXXXXXX
VOBIZ_PUBLIC_BASE_URL=https://your-domain.com

# WhatsApp (OpenWA)
OPENWA_ENABLED=1
OPENWA_API_URL=http://127.0.0.1:2785
OPENWA_API_KEY=your_openwa_key
OPENWA_SESSION_ID=your_session_id

# Features
RAG_ENABLED=true
CALL_RECORDING_ENABLED=true
CONVERSATION_LOG_ENABLED=true
CAMPAIGN_QUIET_HOURS_ENABLED=false
WHATSAPP_INBOUND_LEADS_ENABLED=1
```

### 2. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

## Call Flow

1. **Campaign dials lead** → Vobiz telephony connects → Gemini Live conversation starts
2. **Agent converses** → Follows system prompt (offers details, asks for callback time, offers virtual meet)
3. **Call ends** → Audio recording saved → Transcription via Gemini 2.5 Flash
4. **Analysis runs** → Gemini analyzes transcript for disposition, rating, emotion, next actions
5. **Auto-actions trigger**:
   - If customer asked for WhatsApp details → project details sent automatically via OpenWA
   - If customer gave callback time → `scheduled_callbacks` entry created → campaign auto-dials at that time
6. **Dashboard updates** → Transcript, rating, emotion, disposition all visible in real-time

## Callback System

When the AI agent detects a callback request:

1. `callback_reminder_epoch` extracted from analysis
2. Lead status set to `callback_scheduled`
3. Entry added to `scheduled_callbacks` table
4. Campaign worker checks `get_next_immediate_callback()` each loop iteration
5. When callback is due → `_execute_scheduled_callback()` creates a NEW lead + dials
6. Original lead marked as `callback_completed` (visible in dashboard)
7. New lead gets full transcript + analysis like any other call

## Multi-Role System

| Role | Dashboard | Leads | Campaign |
|------|-----------|-------|----------|
| `sales_1` | Sandbox 1 — Sales 1 | Own CSV upload | Separate campaign |
| `sales_2` | Sandbox 2 — Sales 2 | Own CSV upload | Separate campaign |
| `maruti` | Admin (defaults to Sales 1 view) | — | — |

All queries filter by `role` column. Complete data isolation between Sales 1 and Sales 2.

## Dashboard

Access at: `http://your-server:8000/console`

- **Stats Bar** — Total leads, called, interested, not interested, callbacks, failed, conversion rate
- **Lead Manifest** — Sortable table with status badges, click to view details
- **Call Detail Modal** — Recording playback, outcome, rating (1-5 stars), summary, emotion, next steps, date, location, budget, full transcript
- **Re-analyze** — Re-run Gemini analysis on any completed call
- **Campaign Controls** — Start/stop campaign, upload leads, configure phone numbers

## License

Private — Uday Auto Link
