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

## Deployment & Integration Modes

Hermes is hardware-agnostic and supports three universal deployment architectures:

### Mode A: Embedded All-in-One (Orama + Hermes on One Host)
Hermes is natively mounted into Orama's Flask web server on a single host/port:
- **URL**: `http://<orama-host>:8765/hermes/`
- **Navigation**: Click **Hermes** in Orama's top navigation bar.
- **Zero-Config Authentication**: Logged-in Orama users are authenticated automatically via session cookies with CSRF protection; no manual API key entry is required.
- **Single Port**: Everything runs under Orama's default port `8765`.

### Mode B: Distributed Companion Node (e.g. Server on NAS + Hermes on Mini-PC/VM)
Run Orama on your primary server/NAS and offload LLM inference and TTS speech synthesis to a dedicated companion node (Mini-PC, secondary server, or VM):
1. **On Orama Server (NAS)**: Set `HERMES_URL=http://<companion-ip>:8787` in `orama.env`. When users click "Hermes" in Orama's navbar, it seamlessly redirects to the companion node.
2. **On Hermes Node**: Run `docker compose -f docker-compose.hermes.yml up -d` with `ORAMA_URL=http://<nas-ip>:8765` and `PLEX_URL=http://<nas-ip>:32400`.
3. Automated deployment scripts:
   - Linux: `./scripts/deploy_hermes_minipc.sh`
   - Windows PowerShell: `./scripts/deploy_hermes_minipc.ps1`

### Mode C: Standalone Assistant (Independent Media Intelligence)
Hermes runs standalone on any machine with zero dependency on Orama:
- Directly connects to Plex, Jellyfin, and TMDB.
- Delivers voice/chat assistant and personalized catalogue recommendations.
- Set `ORAMA_URL=""` if you do not run Orama.

---

## Universal LLM & Voice Provider Support

Hermes connects to any combination of cloud and local inference engines:
- **Google Gemini**: High-speed conversational reasoning and Gemini Neural TTS (`GEMINI_API_KEY`, `gemini-2.5-flash`).
- **OpenAI-Compatible Endpoints**: Connect to OpenAI, OpenRouter, Groq, vLLM, LM Studio, or LocalAI via standard OpenAI API format (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`).
- **Local Ollama**: 100% offline private local LLMs (`OLLAMA_URL`, `OLLAMA_MODEL=llama3.1:8b`). Optional NVIDIA GPU passthrough support included in Compose.
- **Local Piper TTS**: CPU-only offline neural voice models with Greek (`rapunzelina`) and English (`lessac`) support.

---

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `NODE_NAME` | Friendly node identifier shown in telemetry and dashboard | `Hermes Companion Node` |
| `HERMES_API_KEY` | Secret bearer token required for `/v1/` routes (min 32 chars) | *Required for standalone* |
| `HERMES_BIND_IP` | Host IP binding | `127.0.0.1` (`0.0.0.0` for LAN) |
| `ORAMA_URL` | Base URL of Orama Server for request links and health probing | `http://orama:8765` |
| `HERMES_URL` | (Orama server setting) Remote Hermes companion URL to redirect navbar clicks | *Optional* |
| `PLEX_URL` | Private LAN/container URL to Plex Media Server | *Optional* |
| `PLEX_TOKEN` | Plex authentication token | *Optional* |
| `JELLYFIN_URL` | Private LAN/container URL to Jellyfin | *Optional* |
| `JELLYFIN_API_KEY` | Jellyfin API key | *Optional* |
| `JELLYFIN_USER_ID` | Jellyfin user GUID | *Optional* |
| `TMDB_API_KEY` | TMDB v3 API key for media discovery | *Required for discovery* |
| `GEMINI_API_KEY` | Google Gemini API key for chat, reranking, and neural TTS | *Optional* |
| `GEMINI_MODEL` | Gemini conversational model | `gemini-2.5-flash` |
| `GEMINI_TTS_MODEL` | Gemini text-to-speech model | `gemini-3.8-flash-tts` |
| `OPENAI_BASE_URL` | Base URL for OpenAI-compatible LLM endpoints | `https://api.openai.com/v1` |
| `OPENAI_API_KEY` | API key for OpenAI, OpenRouter, Groq, etc. | *Optional* |
| `OPENAI_MODEL` | OpenAI-compatible model name | `gpt-4o-mini` |
| `OLLAMA_URL` | Private URL to Ollama inference engine | `http://ollama:11434` |
| `OLLAMA_MODEL` | Ollama model name | `llama3.1:8b` |
| `INSTALL_LOCAL_TTS` | Install Piper TTS in Docker image | `true` |
| `PIPER_MODEL_DIR` | Directory containing `.onnx` and `.onnx.json` voice models | `/voices` |
| `REFRESH_HOURS` | Discovery cache refresh interval | `24` |

---

## API Endpoints

All `/v1/` routes accept Bearer token authentication or active Orama session cookies:

- `GET /healthz`: Public service liveness probe.
- `GET /v1/config`: Public configuration metadata (returns `orama_url` and `node_name`).
- `GET /v1/recommendations`: Retrieve cached movie and series recommendations.
- `POST /v1/refresh`: Trigger an on-demand recommendation refresh against Plex/Jellyfin/TMDB.
- `POST /v1/chat`: Conversational message exchange (JSON request/response).
- `POST /v1/chat/stream`: Streamed conversational tokens (NDJSON).
- `GET /v1/voices`: List available Gemini neural voices and installed local Piper voices.
- `POST /v1/tts/stream`: Stream synthesized audio chunks (NDJSON with base64 WAV buffers).
- `GET /v1/status`: Read-only connection probes for Orama, Plex, Jellyfin, Gemini, OpenAI, Ollama, and NAS.
