# System Workflows

## 1. Outbound Call Flow (Campaign)

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. LEAD UPLOAD                                                      │
│    POST /api/campaign/upload (CSV/XLSX)                             │
│    → core/worker.py: parse leads → INSERT INTO leads (status=pending)│
│    → Dashboard: shows lead count, ready to start                    │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. CAMPAIGN START                                                   │
│    POST /api/campaign/start                                         │
│    → core/worker.py: start_campaign_worker()                        │
│    → Loop: pick next pending lead → check quiet hours → dial        │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. DIAL (per lead)                                                  │
│    core/worker.py: process_lead()                                   │
│    → Update lead status: dialing                                    │
│    → make_vobiz_call(phone, role, lead_id)                          │
│    → Vobiz API: POST /calls (record=true, callback URL)             │
│    → Vobiz dials the phone number                                   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. ANSWER                                                           │
│    Vobiz → POST /vobiz/answer?camp_id=...                           │
│    → backend/api/routes/vobiz.py: vobiz_answer()                    │
│    → Return XML: <Stream bidirectional="true">                      │
│       wss://srv1782910.hstgr.cloud/ws/vobiz?camp_id=...&manual_role=│
│    → Vobiz opens WebSocket to /ws/vobiz                             │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. WEBSOCKET BRIDGE (THE CRITICAL PATH)                            │
│    backend/services/vobiz_bridge/__init__.py                        │
│    handle_vobiz_ws_live()                                           │
│                                                                     │
│    a) Parse WS URL params → resolve role, camp_id, lead_id         │
│    b) Load greeting PCM (cached or generated)                       │
│    c) Open Gemini Live session (models/gemini-3.1-flash-live-preview)│
│    d) Send Gemini setup: voice=Leda, inputAudioTranscription,       │
│       outputAudioTranscription, automaticActivityDetection          │
│                                                                     │
│    Three parallel tasks:                                            │
│    ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐│
│    │ audio_reader()   │  │ audio_sender()   │  │ playback_loop()  ││
│    │ Reads Gemini WS  │  │ Reads Vobiz WS   │  │ Reads Gemini out ││
│    │ → transcripts    │  │ → sends to Gemini││ → resamples to    ││
│    │ → model_speaking │  │ → Hybrid VAD gate │  │   16kHz → Vobiz  ││
│    └──────────────────┘  └──────────────────┘  └──────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 6. GREETING PLAYBACK                                                │
│    play_opening_pcm_stream(pcm_raw, raw_sr)                         │
│    → Resample greeting to 16kHz                                     │
│    → Send 40ms chunks with sleep(0.038) pacing                      │
│    → Each chunk: playAudio event → Vobiz WS                         │
│    → Also writes to agent_pcm for recording                         │
│    → Greeting gate: blocks caller audio for greeting_duration        │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 7. CONVERSATION LOOP                                                │
│                                                                     │
│    CALLER AUDIO PATH:                                               │
│    Vobiz WS → audio_sender()                                        │
│    → Hybrid VAD: energy gate (threshold=600, 3 chunks to arm)       │
│    → Blind while model_speaking=True + 250ms echo guard             │
│    → After 300ms silence: send audioStreamEnd → Gemini finalizes    │
│    → Forward to Gemini WS: realtimeInput.audio (16kHz PCM)          │
│                                                                     │
│    AGENT AUDIO PATH:                                                │
│    Gemini WS → audio_reader()                                       │
│    → Parse mimeType → extract rate (default 24kHz)                  │
│    → Put ("audio", pcm_data, rate) in out_q                         │
│    → playback_loop() reads from out_q                               │
│    → _resample_contiguous(): 4ms context polyphase FIR              │
│    → 40ms frames → play_audio() with sleep(0.038) pacing           │
│    → playAudio event → Vobiz WS → caller hears agent                │
│                                                                     │
│    turn_complete:                                                   │
│    → Flush remaining buffered audio                                 │
│    → Reset prev_tail (no cross-turn artifact)                       │
│    → Send checkpoint event to Vobiz                                 │
│    → Set playing=False                                              │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 8. HANGUP                                                           │
│    Vobiz → POST /vobiz/hangup                                       │
│    → core/worker.py: handle_hangup()                                │
│                                                                     │
│    a) Drain pending agent audio (_pb_state dict)                    │
│    b) Apply 5ms fade-out to prevent click at end                   │
│    c) Save recording: _save_call_recording_wav()                    │
│       → Mix caller_pcm + agent_pcm → WAV file                      │
│       → Optional: encode to MP3 via lameenc                         │
│    d) Store transcript in DB                                        │
│    e) Trigger post-call analysis                                    │
│    f) Execute auto-actions (WhatsApp, email, callback)              │
│    g) Update lead status in DB                                      │
│    h) Fire webhook_url with full payload                            │
└─────────────────────────────────────────────────────────────────────┘
```

## 2. Audio Recording Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│ DURING CALL                                                         │
│                                                                     │
│ caller_pcm (bytearray)              agent_pcm (bytearray)           │
│ ← Vobiz WS audio frames            ← _emit() writes resampled PCM  │
│ ← 16kHz mono PCM16                  ← 16kHz mono PCM16              │
│ ← Growing in real-time              ← _pad_agent_realtime() gaps    │
│                                                                     │
│ Both are capped at ~2 minutes (CALLER_CAP, AGENT_CAP)               │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ AT HANGUP                                                           │
│                                                                     │
│ _save_call_recording_wav(role, camp_id, caller_pcm, agent_pcm)      │
│                                                                     │
│ 1. Mix sample-by-sample:                                            │
│    mixed[i] = caller[i] + int(agent[i] * 0.7)                      │
│    Clamp to [-32768, 32767]                                         │
│                                                                     │
│ 2. Write WAV:                                                       │
│    → 16kHz, mono, 16-bit PCM                                        │
│    → path: call_recordings/<role>/<camp_id>.wav                      │
│                                                                     │
│ 3. Encode MP3 (optional):                                           │
│    → lameenc: 96kbps, quality 5                                     │
│    → path: call_recordings/<role>/<camp_id>.mp3                      │
│    → Returns MP3 path (fallback to WAV)                             │
└─────────────────────────────────────────────────────────────────────┘
```

## 3. Gemini Live Session Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│ SETUP (once per call)                                               │
│                                                                     │
│ WebSocket: wss://generativelanguage.googleapis.com/...              │
│                                                                     │
│ Send JSON:                                                          │
│ {                                                                   │
│   "setup": {                                                        │
│     "model": "models/gemini-3.1-flash-live-preview",                │
│     "generationConfig": {                                           │
│       "responseModalities": ["AUDIO"],                              │
│       "speechConfig": {"voiceConfig": {"voiceName": "Leda"}}        │
│     },                                                              │
│     "realtimeInputConfig": {                                        │
│       "automaticActivityDetection": {                               │
│         "prefixPaddingMs": 30,                                      │
│         "silenceDurationMs": 450,                                   │
│         "startOfSpeechSensitivity": "HIGH",                         │
│         "endOfSpeechSensitivity": "HIGH"                            │
│       }                                                             │
│     },                                                              │
│     "inputAudioTranscription": {},                                  │
│     "outputAudioTranscription": {},                                 │
│     "systemInstruction": {"parts": [{"text": "..."}]}               │
│   }                                                                 │
│ }                                                                   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ LIVE SESSION                                                         │
│                                                                     │
│ OUTBOUND (to Gemini):                                               │
│ {                                                                   │
│   "realtimeInput": {                                                │
│     "audio": {                                                      │
│       "data": "<base64 PCM16>",                                     │
│       "mimeType": "audio/pcm;rate=16000"                            │
│     }                                                               │
│   }                                                                 │
│ }                                                                   │
│                                                                     │
│ INBOUND (from Gemini):                                              │
│ {                                                                   │
│   "serverContent": {                                                │
│     "modelTurn": {                                                  │
│       "parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000"}}]│
│     },                                                              │
│     "turnComplete": true/false                                      │
│   }                                                                 │
│ }                                                                   │
│                                                                     │
│ TRANSCRIPTIONS:                                                     │
│ {                                                                   │
│   "serverContent": {                                                │
│     "inputTranscription": {"text": "caller said..."},               │
│     "outputTranscription": {"text": "agent said..."}                │
│   }                                                                 │
│ }                                                                   │
└─────────────────────────────────────────────────────────────────────┘
```

## 4. Hybrid VAD (Voice Activity Detection)

```
┌─────────────────────────────────────────────────────────────────────┐
│ ENERGY GATE (in audio_sender)                                       │
│                                                                     │
│ Parameters:                                                         │
│   ENERGY_RMS = 600 (threshold for speech detection)                 │
│   ARM_CHUNKS = 3 (sustained chunks needed to arm)                   │
│   AGENT_ECHO_GUARD_S = 0.25 (blind after agent audio stops)         │
│   hybrid_end_ms = 300 (silence to finalize turn)                    │
│                                                                     │
│ States:                                                             │
│   IDLE → ARMED → SPEAKING → SILENCE → FINALIZED                    │
│                                                                     │
│ Logic:                                                              │
│   if model_speaking:                                                │
│       → BLIND (agent voice playing → energy is echo)                │
│       → Reset arm_run, silence_ms                                   │
│   elif last_agent_active + 0.25s:                                   │
│       → BLIND (echo guard period)                                   │
│   elif rms > 600:                                                   │
│       → arm_run += 1                                                │
│       → if arm_run >= 3: speaking = True                            │
│   elif speaking:                                                    │
│       → silence_ms += chunk_ms                                      │
│       → if silence_ms >= 300: send audioStreamEnd                   │
│       → speaking = False                                            │
│                                                                     │
│ Result: Gemini finalizes turn IMMEDIATELY instead of waiting for     │
│ server-side end-of-speech timer → cuts answer latency               │
└─────────────────────────────────────────────────────────────────────┘
```

## 5. Audio Resampling Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│ GEMINI OUTPUT (24kHz PCM16)                                         │
│                                                                     │
│ playback_loop():                                                    │
│   1. Accumulate in `buffered` until ≥ FLUSH_BYTES (100ms = 4800B)   │
│   2. _resample_contiguous():                                        │
│      a) combined = prev_tail + buffered                             │
│      b) pcm_resample(combined, 24kHz → 16kHz)                      │
│         - Polyphase FIR: Kaiser window β=8.6, 12 zero-crossings    │
│         - Upsample by 2 (P), filter, decimate by 3 (Q)             │
│      c) Drop first `drop` bytes (context output)                    │
│      d) prev_tail = combined[-CTX_BYTES:] (4ms = 192 bytes)        │
│   3. Extend `carry` with resampled output                           │
│   4. While carry ≥ FRAME_BYTES (40ms = 1280B):                     │
│      → _emit(frame) → play_audio(frame, 16000)                     │
│      → play_audio sends 1 chunk + sleep(0.038)                     │
│                                                                     │
│ VOBIZ RECEIVES:                                                     │
│   40ms frames at 38ms intervals (realtime pacing)                   │
│   ContentType: audio/x-l16, SampleRate: 16000                      │
└─────────────────────────────────────────────────────────────────────┘
```

## 6. Post-Call Analysis

```
┌─────────────────────────────────────────────────────────────────────┐
│ TRIGGER: Hangup event                                               │
│                                                                     │
│ core/worker.py: analyze_call(camp_id, lead_id, role)                │
│                                                                     │
│ 1. Transcription (services/transcriber.py)                          │
│    → Gemini 2.5 Flash on agent_pcm (16kHz WAV)                      │
│    → Returns: full transcript, segments, timestamps                 │
│                                                                     │
│ 2. Analysis (services/call_analyzer.py)                             │
│    → Gemini 2.5 Flash on transcript + prompt                        │
│    → Returns: disposition, rating, emotion, summary, callbacks      │
│                                                                     │
│ 3. Storage:                                                         │
│    → UPDATE leads SET analysis=..., transcript=... WHERE id=lead_id │
│    → INSERT INTO manual_calls (if manual call)                       │
│                                                                     │
│ 4. Auto-actions:                                                    │
│    → If disposition="interested": send WhatsApp + email              │
│    → If callback_requested: schedule callback                        │
│    → Update lead status: completed                                   │
│                                                                     │
│ 5. Webhook:                                                         │
│    → POST to webhook_url with full payload                          │
│    → Includes: recording_url, summary, transcript, analysis         │
└─────────────────────────────────────────────────────────────────────┘
```

## 7. Dashboard Real-Time Updates

```
┌─────────────────────────────────────────────────────────────────────┐
│ SSE (Server-Sent Events)                                            │
│                                                                     │
│ GET /api/events/stream → EventSourceResponse                        │
│                                                                     │
│ Event types:                                                        │
│   - campaign_update: lead count, status changes                     │
│   - call_update: new call started/ended                             │
│   - lead_update: individual lead status change                      │
│                                                                     │
│ Backend:                                                            │
│   core/events.py: EventBus                                          │
│   → pub/sub pattern                                                 │
│   → Workers publish events                                          │
│   → SSE endpoint subscribes and streams to browser                  │
│                                                                     │
│ Frontend:                                                           │
│   static/js/app.js: EventSource('/api/events/stream')               │
│   → Updates DOM in real-time                                        │
│   → No polling needed                                               │
└─────────────────────────────────────────────────────────────────────┘
```

## 8. WhatsApp Integration (Dual-Path)

```
┌─────────────────────────────────────────────────────────────────────┐
│ PATH 1: Meta WhatsApp Cloud API (Official)                          │
│                                                                     │
│ Inbound:                                                            │
│   POST /api/whatsapp/webhook (Meta verification + messages)         │
│   → Parse message → store in conversation history                   │
│   → Auto-respond if configured                                      │
│                                                                     │
│ Outbound:                                                           │
│   services/whatsapp_leads.py                                        │
│   → POST to Graph API: /v21.0/{phone_id}/messages                   │
│   → Send text, template, media                                      │
│   → Track delivery status via webhook                               │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PATH 2: OpenWA Gateway (WhatsApp Web Pairing)                       │
│                                                                     │
│ Setup:                                                              │
│   GET /dariaan/whatsapp → QR code page                              │
│   → User scans QR with WhatsApp                                     │
│   → OpenWA session authenticated                                    │
│                                                                     │
│ Inbound:                                                            │
│   POST /api/whatsapp/proxy/message (from OpenWA)                    │
│   → AI analysis + auto-respond                                      │
│                                                                     │
│ Outbound:                                                           │
│   POST /api/whatsapp/proxy/send                                     │
│   → Forward to OpenWA API                                           │
│   → OpenWA sends via WhatsApp Web protocol                          │
└─────────────────────────────────────────────────────────────────────┘
```

## 9. RAG (Retrieval-Augmented Generation)

```
┌─────────────────────────────────────────────────────────────────────┐
│ STORAGE: SQLite FTS5 (not vector DB)                                │
│                                                                     │
│ backend/rag.py                                                      │
│   - Chunks stored with heading hierarchy                            │
│   - Full-text search via FTS5 MATCH                                 │
│   - Per-role isolation (sales_1 vs sales_2)                         │
│                                                                     │
│ INDEXING:                                                           │
│   POST /api/rag/reindex                                             │
│   → Parse RAG text → split into chunks                              │
│   → Extract headings (H1-H4) → store hierarchy                      │
│   → INSERT INTO rag_chunks (role, heading, content, chunk_idx)      │
│                                                                     │
│ RETRIEVAL:                                                          │
│   During call: worker.py calls rag.query(text, role)                │
│   → FTS5 search on caller's speech                                  │
│   → Return top 3 matching chunks                                    │
│   → Inject into Gemini system prompt as context                     │
│                                                                     │
│ UI:                                                                  │
│   POST /api/rag → Save RAG text                                     │
│   GET /api/rag → Get RAG text + chunk stats                         │
│   POST /api/rag/upload → Upload document                            │
│   GET /api/rag/query → Preview chunk retrieval                      │
└─────────────────────────────────────────────────────────────────────┘
```

## 10. Deployment Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│ LOCAL DEVELOPMENT                                                    │
│                                                                     │
│ cd backend && python -m uvicorn main:app --reload --port 8000       │
│                                                                     │
│ .env file: GEMINI_API_KEY, VOBIZ_*, DATABASE_URL, etc.              │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ DEPLOY TO VPS                                                        │
│                                                                     │
│ 1. Push to GitHub:                                                  │
│    git push origin master                                           │
│                                                                     │
│ 2. SSH to VPS:                                                      │
│    ssh -i key root@187.127.187.12                                   │
│                                                                     │
│ 3. Pull and rebuild:                                                │
│    cd /opt/ai_agent                                                 │
│    git pull origin master                                           │
│    docker compose down                                              │
│    docker compose up -d --build                                     │
│                                                                     │
│ 4. Verify:                                                          │
│    docker ps --filter name=vernika-bridge                           │
│    docker logs vernika-bridge --tail 50                             │
│                                                                     │
│ VPS: 187.127.187.12                                                 │
│ Public URL: https://srv1782910.hstgr.cloud                          │
│ Nginx SSL → localhost:8000                                          │
└─────────────────────────────────────────────────────────────────────┘
```
