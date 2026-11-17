# Orama Server

Orama Server sets up and looks after a home media server for you. You run one command on a
Debian, Ubuntu or UGREEN (UGOS Pro) machine, answer a few questions in your browser, and Orama
installs and connects the apps: Plex and/or Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd,
qBittorrent and a VPN. After that you search for a movie or a show in Orama, ask for it, and
watch its progress until it is ready to play, with subtitles.

## Install

On the server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/DarkAngelGR/orama-server/main/install.sh | sudo bash
```

The installer ([`install.sh`](install.sh)) uses the Docker that is already there (or installs it
on plain Debian/Ubuntu), starts Orama, and prints a one-time setup link. Open that link in a
browser to choose a password and finish the setup. Running the installer again is safe: it only
repairs what is missing.

## Architecture & Deployment Topologies

Orama Server is designed to fit your home hardware setup flexibly, supporting both single-server installs and distributed multi-machine deployments:

### 1. Single-Server (All-in-One)
Everything runs on one machine (such as your NAS or a dedicated server). Orama, the media stack, and Hermes assistant run side-by-side:
- **Port**: `8765` for Orama Server (with Hermes embedded at `/hermes`).
- **Best for**: Powerful servers or NAS devices with sufficient CPU/RAM for all apps.

### 2. Distributed Co-Op (Storage on NAS + Intelligence on Companion Mini-PC)
Offload compute-intensive tasks to keep your storage NAS cool, quiet, and responsive:
- **Primary Storage (NAS)**: Runs Orama Server (`:8765`), Plex/Jellyfin (`:32400` / `:8096`), Sonarr, Radarr, and download clients.
- **Companion Node (Mini-PC / Secondary PC)**: Runs Hermes Assistant (`:8787`), local LLM inference via Ollama (`:11434`), and CPU-only Piper neural voice synthesis.
- **Seamless LAN Navigation**: In Orama, open Settings → Hermes and save the mini PC address, for example `http://mini-pc.local:8787`. Clicking **Hermes** then opens that dashboard.
- **Direct 1-Click Requests**: Media recommendations discovered by Hermes on the Mini-PC link directly back to Orama on the NAS (`http://nas.local:8765/media/<kind>/<id>`) for automated retrieval.

```
 ┌──────────────────────────────────────┐        LAN / Wi-Fi       ┌─────────────────────────────────────┐
 │        Storage NAS (e.g. TrueNAS)    │ ◄──────────────────────► │    Companion Mini-PC / Secondary    │
 ├──────────────────────────────────────┤                          ├─────────────────────────────────────┤
 │ • Orama Web Server (:8765)           │                          │ • Hermes Voice Assistant (:8787)    │
 │ • Media Storage & Download Pipeline  │                          │ • Local Ollama LLM (:11434)         │
 │ • Plex / Jellyfin (:32400 / :8096)   │                          │ • Piper Local Neural Voices (CPU)   │
 │ • Sonarr, Radarr, qBittorrent, VPN   │                          │ • Bilingual English & Greek Voice   │
 └──────────────────────────────────────┘                          └─────────────────────────────────────┘
```

---

## Universal AI & Voice Provider Configuration

Hermes can connect to your preferred mix of cloud and self-hosted AI engines:

| Provider | Supported Models | Configuration | Use Case |
|---|---|---|---|
| **Google Gemini** | `gemini-2.5-flash`, `gemini-3.8-flash-tts` | `GEMINI_API_KEY` | Ultra-fast conversational reasoning and neural voice |
| **OpenAI-Compatible** | Any standard endpoint (OpenAI, OpenRouter, Groq, vLLM, LM Studio, LocalAI) | `OPENAI_BASE_URL`<br>`OPENAI_API_KEY`<br>`OPENAI_MODEL` | Universal compatibility across any LLM backend or private model server |
| **Local Ollama** | `llama3.1:8b`, `mistral`, `qwen2.5`, etc. | `OLLAMA_URL`<br>`OLLAMA_MODEL` | 100% private, offline inference running on CPU or NVIDIA GPU |
| **Local Piper TTS** | Greek (`rapunzelina`), English (`lessac`) | `INSTALL_LOCAL_TTS=true`<br>`PIPER_MODEL_DIR=/voices` | Low-latency, CPU-only local speech synthesis without cloud dependencies |

---

## Settigation Cinema Console Interface

Orama features a cinema console interface built on frosted glass panels floating over ambient projector lighting, with Settigation-style typography and floating pill controls:
- **Floating Pill Navigation**: Centered glass pill header with live connection status and instant **`Request Title ↗`** access.
- **System Telemetry**: Real-time operational microservices counter, playback fidelity mode, and storage status.
- **Live Orchestration Terminal**: Syntax-highlighted codebox console displaying pipeline state and agent activity.
- **Personalized Recommendations**: Media showcase matched to your watch history with one-click Orama requests.

---

## Quick Start: Distributed Deployment

### Step 1: Deploy Orama on the NAS
Run the one-line installer on your NAS:
```bash
curl -fsSL https://raw.githubusercontent.com/DarkAngelGR/orama-server/main/install.sh | sudo bash
```
Open the setup link printed by the installer to configure your administrator password, storage folder, and media apps.

To route Orama's navbar to your Mini-PC, open Orama → Settings → Hermes and save the mini PC address, for example `http://mini-pc.local:8787`. The Hermes link then opens that address.

### Step 2: Deploy Hermes on the Mini-PC
Clone this repository on your Mini-PC and run the automated deployment script:

**Linux / macOS:**
```bash
./scripts/deploy_hermes_minipc.sh
```

**Windows PowerShell:**
```powershell
.\scripts\deploy_hermes_minipc.ps1
```

The script will automatically:
1. Generate a secure 48-character `HERMES_API_KEY`.
2. Download lightweight Piper Greek and English neural voice models into `./voices/`.
3. Launch the container stack via `docker-compose.hermes.yml`.
4. Verify service health on port `8787`.

To customize your AI provider or media credentials manually, edit [`hermes.env.example`](hermes.env.example) and copy it to `hermes.env`.

---

## Working on Orama

- [`AGENTS.md`](AGENTS.md): instructions for anyone (person or AI assistant) contributing to this repository.
- [`docs/HERMES.md`](docs/HERMES.md): detailed architecture, API endpoints, and configuration for Hermes.
- [`docs/STATUS.md`](docs/STATUS.md): current roadmap, task states, and verification progress.
- [`docs/OWNER-CHECKLIST.md`](docs/OWNER-CHECKLIST.md): physical verification checklist for NAS hardware.
- [`docs/superpowers/specs/2026-11-03-orama-release-1-design.md`](docs/superpowers/specs/2026-11-03-orama-release-1-design.md): Release 1 architecture and security specifications.
