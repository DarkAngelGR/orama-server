# Hermes — Personal Media Assistant & Recommendation Engine for Orama

**Hermes (Ερμής)** is the companion assistant and intelligent recommendation engine for Orama Server. Named after the swift messenger and voice of the gods, Hermes bridges conversational voice/chat dialogue with automated media discovery and Orama's request pipeline.

---

## Capabilities

- **Intelligent Media Recommendations**:
  - Pulls watch history, completion states, and user ratings from **Plex** and **Jellyfin**.
  - Fetches fresh discovery candidates from **TMDB** (with fixed outbound URLs, pagination, and size limits).
  - Deterministically ranks titles offline using weighted genre preferences (ratings $\ge 7$ and explicit likes are heavily weighted; continue-watching is neutral).
  - LLM re-ranking (Gemini / Ollama) is strictly bounded: models may only reorder vetted candidate IDs, never inject fabricated titles, summaries, or HTML.
- **Direct Orama Request Integration**:
  - Recommendation cards in Hermes include direct **"Request in Orama ↗"** buttons opening Orama's native `/media/<kind>/<source_id>` request and detail pages.
- **Bilingual Conversational Voice & Chat**:
  - Natural conversation in **English** (`en-US`) and **Greek** (`el-GR`).
  - Streaming LLM inference: Gemini primary with automatic fallback to local Ollama on the UN1250 mini-PC.
  - Server-side neural speech: Gemini neural voice with interruptible, CPU-only **Piper** local TTS fallback.
  - Web Audio decoding with push-to-talk, toggle microphone, and real-time audio visualization.
- **Connection & Reachability Probing**:
  - Read-only diagnostics for Plex, Jellyfin, Ollama, Orama Server, and NAS reachability.

---

## Project Structure

```
src/hermes/
├── __init__.py           # Package initialization
├── app.py                # FastAPI endpoints, auth, cache & background sync
├── conversation.py       # Gemini & Ollama streaming chat adapters
├── piper_worker.py       # Isolated worker process for local Piper TTS
├── providers.py          # Plex, Jellyfin, and TMDB media discovery adapters
├── recommend.py          # Deterministic ranking and model reordering
├── status.py             # Read-only service reachability probes (including Orama)
├── streams.py            # NDJSON event streaming utilities
├── voice.py              # Gemini neural voice & Piper TTS pipeline
└── static/               # Standalone frontend dashboard (HTML/CSS/JS)
    ├── index.html
    ├── app.js
    ├── core.js
    ├── fonts.css
    └── style.css
```

---

## Deployment & Configuration

### Docker Compose
Run Hermes using `docker-compose.hermes.yml`:

```bash
docker compose -f docker-compose.hermes.yml build hermes
docker compose -f docker-compose.hermes.yml up -d hermes
```

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `HERMES_API_KEY` | Secret bearer token required for `/v1/` routes (min 32 chars) | *Required* |
| `HERMES_BIND_IP` | Host IP binding | `127.0.0.1` |
| `ORAMA_URL` | Base URL of Orama Server for request links and health probing | `http://orama:8765` |
| `PLEX_URL` | Private LAN URL to Plex Media Server | *Optional* |
| `PLEX_TOKEN` | Plex authentication token | *Optional* |
| `JELLYFIN_URL` | Private LAN URL to Jellyfin | *Optional* |
| `JELLYFIN_API_KEY` | Jellyfin API key | *Optional* |
| `JELLYFIN_USER_ID` | Jellyfin user GUID | *Optional* |
| `TMDB_API_KEY` | TMDB v3 API key for media discovery | *Required for discovery* |
| `GEMINI_API_KEY` | Google Gemini API key for chat, reranking, and neural TTS | *Optional* |
| `GEMINI_MODEL` | Gemini conversational model | `gemini-2.5-flash` |
| `GEMINI_TTS_MODEL` | Gemini text-to-speech model | `gemini-3.8-flash-tts` |
| `OLLAMA_URL` | Private URL to Ollama inference engine | `http://ollama:11434` |
| `OLLAMA_MODEL` | Ollama model name | `llama3.1:8b` |
| `INSTALL_LOCAL_TTS` | Install Piper TTS in Docker image | `true` |
| `PIPER_MODEL_DIR` | Directory containing `.onnx` and `.onnx.json` voice models | `/voices` |
| `REFRESH_HOURS` | Discovery cache refresh interval | `24` |

---

## API Endpoints

All `/v1/` routes require `Authorization: Bearer <HERMES_API_KEY>`.

- `GET /healthz`: Public service liveness probe.
- `GET /v1/config`: Public configuration metadata (returns configured `orama_url`).
- `GET /v1/recommendations`: Retrieve cached movie and series recommendations.
- `POST /v1/refresh`: Trigger an on-demand recommendation refresh against Plex/Jellyfin/TMDB.
- `POST /v1/chat`: Conversational message exchange (JSON request/response).
- `POST /v1/chat/stream`: Streamed conversational tokens (NDJSON).
- `GET /v1/voices`: List available Gemini neural voices and installed local Piper voices.
- `POST /v1/tts/stream`: Stream synthesized audio chunks (NDJSON with base64 WAV buffers).
- `GET /v1/status`: Read-only connection probes for Orama, Plex, Jellyfin, Ollama, and NAS.
