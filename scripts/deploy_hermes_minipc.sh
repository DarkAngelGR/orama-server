#!/usr/bin/env bash
# ==============================================================================
# Hermes 2.0 Deployment Script for Mini-PC (Linux / Docker)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "=== Hermes 2.0 Deployment on Mini-PC ==="

# 1. Check Docker & Compose
if ! command -v docker &> /dev/null; then
  echo "Error: 'docker' is not installed or not in PATH." >&2
  exit 1
fi

COMPOSE_CMD=""
if docker compose version &> /dev/null; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
  COMPOSE_CMD="docker-compose"
else
  echo "Error: Neither 'docker compose' nor 'docker-compose' was found." >&2
  exit 1
fi

# 2. Setup hermes.env
if [ ! -f "hermes.env" ]; then
  if [ -f "hermes.env.example" ]; then
    echo "Creating hermes.env from hermes.env.example..."
    cp hermes.env.example hermes.env
    
    # Generate secure random API key
    if command -v openssl &> /dev/null; then
      RAND_KEY=$(openssl rand -hex 24)
      sed -i "s/change_me_to_at_least_32_random_characters_secret_key/$RAND_KEY/" hermes.env
      echo "Generated random HERMES_API_KEY in hermes.env"
    fi
    echo "NOTICE: Please edit hermes.env to fill in your ORAMA_URL, PLEX_URL, and API keys."
  fi
fi

# 3. Create voices and data directories
mkdir -p voices

# 4. Optional: Download Greek and English Piper voice models if voices dir is empty
if [ -z "$(ls -A voices 2>/dev/null)" ]; then
  echo "Voices directory is empty. Downloading lightweight Greek and English neural voices..."
  PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
  
  # Greek voice (rapunzelina)
  curl -sSL -o voices/el_GR-rapunzelina-low.onnx "$PIPER_BASE/el/el_GR/rapunzelina/low/el_GR-rapunzelina-low.onnx" || true
  curl -sSL -o voices/el_GR-rapunzelina-low.onnx.json "$PIPER_BASE/el/el_GR/rapunzelina/low/el_GR-rapunzelina-low.onnx.json" || true

  # English voice (lessac)
  curl -sSL -o voices/en_US-lessac-medium.onnx "$PIPER_BASE/en/en_US/lessac/medium/en_US-lessac-medium.onnx" || true
  curl -sSL -o voices/en_US-lessac-medium.onnx.json "$PIPER_BASE/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json" || true

  echo "Downloaded Piper voices into ./voices."
fi

# 5. Build and Launch
# hermes.env is not used for the published port unless Compose is told to read it.
# Without this, Hermes stays on localhost and the LAN address never opens.
COMPOSE_ENV=()
if [ -f hermes.env ]; then
  COMPOSE_ENV=(--env-file hermes.env)
fi
if [ -z "${HERMES_BIND_IP:-}" ] && ! grep -q '^HERMES_BIND_IP=.' hermes.env 2>/dev/null; then
  export HERMES_BIND_IP=0.0.0.0
fi
echo "Building and starting Hermes container..."
$COMPOSE_CMD "${COMPOSE_ENV[@]}" -f docker-compose.hermes.yml build hermes
$COMPOSE_CMD "${COMPOSE_ENV[@]}" -f docker-compose.hermes.yml up -d hermes

# 6. Verify Health
echo "Waiting for Hermes to be ready on port 8787..."
for i in {1..15}; do
  if curl -sf http://127.0.0.1:8787/healthz &> /dev/null; then
    echo "✓ Hermes is healthy and running!"
    break
  fi
  sleep 2
done

# 7. Print Summary
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
echo ""
echo "=========================================================="
echo " Hermes Assistant is running on your Mini-PC!"
echo " On this computer: http://localhost:8787/"
if curl -sf --max-time 3 "http://${HOST_IP}:8787/healthz" >/dev/null; then
  echo " From other devices: http://${HOST_IP}:8787/"
else
  echo " Other devices cannot open Hermes yet. Port 8787 is not reachable at ${HOST_IP}."
fi
echo "=========================================================="
