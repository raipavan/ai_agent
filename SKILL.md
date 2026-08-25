# OpusHire AI Calling Agent - Developer Skill

## Project Identity
- **Product:** OpusHire / PitchXAI / Vernika Bridge
- **Purpose:** AI-powered voice-calling agent for Maruti Suzuki Arena service center (Uday Auto Links, Ahmedabad)
- **Stack:** FastAPI + PostgreSQL + Gemini Live + Vobiz VoIP + WhatsApp

## Architecture Quick Reference

```
backend/main.py          → Uvicorn entry point (port 8000)
backend/config.py        → Settings dataclass (ALL env vars mapped here)
backend/api/app.py       → FastAPI app factory (create_app)
backend/core/storage.py  → PostgreSQL persistence (ALL DB operations)
backend/core/worker.py   → Campaign dialer loop (outbound calls)
backend/services/vobiz_bridge/__init__.py  → Vobiz↔Gemini Live bridge (THE critical file)
```

## Multi-Role Isolation
- Roles: `sales_1`, `sales_2`, `maruti` (admin defaults to sales_1)
- Each role has isolated: leads, campaigns, prompts, RAG, greetings, Vobiz credentials
- Role resolved from JWT token → passed to all DB/service calls
- Config key pattern: `role_state` table stores per-role settings

## Critical Call Flow
```
1. Campaign worker (core/worker.py) picks lead → calls make_vobiz_call()
2. Vobiz dials → hits /vobiz/answer → returns XML with <Stream> pointing to /ws/vobiz
3. Vobiz opens WebSocket to /ws/vobiz → handle_vobiz_ws_live() takes over
4. Gemini Live session created → greeting played via play_opening_pcm_stream()
5. Caller audio → audio_sender() → Gemini → audio_reader() → playback_loop()
6. playback_loop() resamples Gemini 24kHz → 16kHz → play_audio() → Vobiz WS
7. On hangup: recording saved, transcript stored, analysis triggered, webhook fired
```

## Audio Pipeline (Metallic Sound Fix History)
The metallic/robotic sound was caused by multiple compounding issues:
1. **No pacing in play_audio()** → frames sent in bursts → Vobiz played faster than realtime
2. **Per-flux resampler boundaries** → polyphase FIR treated chunk edges as zeros → discontinuity every 100ms
3. **No echo-proof Hybrid VAD** → false audioStreamEnd mid-response → turn restarts
4. **End-of-call NameError** → final audio lost at hangup

**Current fixes (commits 054ed6c + 122f52d):**
- `play_audio()` now has `sleep(0.038)` per 40ms frame (realtime pacing)
- `_resample_contiguous()` with 4ms source context eliminates boundary discontinuities
- Hybrid VAD gated blind during agent speech + 250ms echo guard
- `_pb_state` dict (NameError-proof) for end-of-call drain

## Key File Locations

### Voice Call System
| File | Lines | Purpose |
|------|-------|---------|
| `backend/services/vobiz_bridge/__init__.py` | ~1500 | Vobiz WS handler, Gemini Live, audio pipeline |
| `backend/services/vobiz_bridge/audio.py` | ~83 | PCM resampling (polyphase FIR + linear fallback) |
| `backend/core/greeting_pcm.py` | ~200 | Greeting PCM recording/caching |
| `backend/core/opening_line.py` | ~100 | Opening line builder |

### Campaign System
| File | Lines | Purpose |
|------|-------|---------|
| `backend/core/worker.py` | ~600 | Dialer loop, lead processing, analysis |
| `backend/core/storage.py` | ~1200 | ALL PostgreSQL operations |
| `backend/api/routes/campaign.py` | ~800 | Campaign REST endpoints |
| `backend/api/routes/console_api.py` | ~1000 | Tuning, RAG, manual calls |

### AI/Analysis
| File | Lines | Purpose |
|------|-------|---------|
| `backend/services/call_analyzer.py` | ~200 | Post-call Gemini analysis |
| `backend/services/transcriber.py` | ~100 | Gemini 2.5 Flash transcription |
| `backend/rag.py` | ~150 | SQLite FTS5 RAG store |
| `backend/prompts/priya.py` | ~100 | System prompts per role |

### Configuration
| File | Purpose |
|------|---------|
| `backend/config.py` | Settings dataclass (all env vars) |
| `.env.production` | Docker env vars |
| `docker-compose.yml` | 3 services: postgres, bridge, openwa |

## Common Development Tasks

### Adding a new API endpoint
1. Create route in `backend/api/routes/<module>.py`
2. Register router in `backend/api/routes/__init__.py`
3. Import in `backend/api/app.py` create_app()

### Modifying the call flow
1. Main logic in `backend/services/vobiz_bridge/__init__.py`
2. Audio processing in `backend/services/vobiz_bridge/audio.py`
3. Greeting handling in `backend/core/greeting_pcm.py`

### Database changes
1. Table creation in `backend/core/storage.py` (init_db)
2. CRUD operations in `backend/core/storage.py`
3. No migration files — schema is code-managed

### Deploying to VPS
```bash
git push origin master
ssh -i <key> root@187.127.187.12 "cd /opt/ai_agent && git pull origin master && docker compose down && docker compose up -d --build"
```

## Environment Variables (Critical Ones)
```bash
# AI
GEMINI_API_KEY=...          # Gemini Live + analysis
GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
GEMINI_LIVE_VOICE=Leda

# Vobiz (per-role in DB, env is fallback)
VOBIZ_AUTH_ID=...
VOBIZ_AUTH_TOKEN=...
VOBIZ_FROM_NUMBER=+1234567890
VOBIZ_PUBLIC_BASE_URL=https://srv1782910.hstgr.cloud

# Database
DATABASE_URL=postgresql://postgres:postgres@db:5432/vernika

# WhatsApp (dual-path)
WHATSAPP_ACCESS_TOKEN=...    # Meta Cloud API
OPENWA_API_URL=...           # OpenWA gateway
OPENWA_API_KEY=...
```

## Testing Checklist
- [ ] Manual call via `/api/manual/call` endpoint
- [ ] Greeting plays correctly (no dropped chunks)
- [ ] Agent response has no metallic/robotic sound
- [ ] Recording saved as WAV (not overwritten by Vobiz MP3)
- [ ] Transcript captured correctly
- [ ] Post-call analysis completes
- [ ] Dashboard updates in real-time (SSE)
- [ ] WhatsApp/email actions triggered correctly

## Rollback Procedure
```bash
# Tag before changes
git tag pre-change HEAD

# If issues, reset to tag
git reset --hard pre-change
git push origin master --force

# VPS
ssh root@187.127.187.12 "cd /opt/ai_agent && git fetch origin && git reset --hard origin/master && docker compose down && docker compose up -d --build"
```

## Known Gotchas
1. **WAV recording overwrite:** Vobiz server-side recording callback overwrites local mix (fixed in commit 72372cc, rolled back)
2. **MP3 encoding artifacts:** lameenc at 96kbps/quality 5 can introduce metallic on16kHz mono
3. **Greeting pacing:** Must be 38ms per 40ms chunk — faster sends get dropped by Vobiz
4. **prev_tail at turn boundaries:** Must be reset to prevent cross-turn resampler artifacts
5. **Static/templates duplication:** Root-level `static/` and `templates/` are dead duplicates — use `frontend/`
