# ==============================================================================
# Hermes 2.0 Deployment Script for Mini-PC (Windows PowerShell)
# ==============================================================================
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "=== Hermes 2.0 Deployment on Mini-PC ===" -ForegroundColor Cyan

# 1. Check Docker
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker is not installed or not in PATH."
    exit 1
}

# 2. Setup hermes.env
if (-not (Test-Path "hermes.env")) {
    if (Test-Path "hermes.env.example") {
        Write-Host "Creating hermes.env from hermes.env.example..." -ForegroundColor Yellow
        Copy-Item "hermes.env.example" "hermes.env"
        
        # Generate random 48-char key
        $chars = (65..90) + (97..122) + (48..57)
        $randKey = -join ($chars | Get-Random -Count 48 | ForEach-Object { [char]$_ })
        (Get-Content "hermes.env") -replace "change_me_to_at_least_32_random_characters_secret_key", $randKey | Set-Content "hermes.env"
        Write-Host "Generated random HERMES_API_KEY in hermes.env" -ForegroundColor Green
        Write-Host "NOTICE: Please edit hermes.env to fill in your ORAMA_URL, PLEX_URL, and API keys." -ForegroundColor Yellow
    }
}

# 3. Create voices directory
if (-not (Test-Path "voices")) {
    New-Item -ItemType Directory -Path "voices" | Out-Null
}

# 4. Optional: Download Greek & English Piper voices if empty
$voicesCount = (Get-ChildItem -Path "voices" -Filter "*.onnx" -ErrorAction SilentlyContinue).Count
if ($voicesCount -eq 0) {
    Write-Host "Voices directory is empty. Downloading lightweight Greek & English neural voices..." -ForegroundColor Yellow
    $baseUri = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

    try {
        Invoke-WebRequest -Uri "$baseUri/el/el_GR/rapunzelina/low/el_GR-rapunzelina-low.onnx" -OutFile "voices\el_GR-rapunzelina-low.onnx"
        Invoke-WebRequest -Uri "$baseUri/el/el_GR/rapunzelina/low/el_GR-rapunzelina-low.onnx.json" -OutFile "voices\el_GR-rapunzelina-low.onnx.json"
        Invoke-WebRequest -Uri "$baseUri/en/en_US/lessac/medium/en_US-lessac-medium.onnx" -OutFile "voices\en_US-lessac-medium.onnx"
        Invoke-WebRequest -Uri "$baseUri/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json" -OutFile "voices\en_US-lessac-medium.onnx.json"
        Write-Host "Downloaded Piper voices into .\voices." -ForegroundColor Green
    } catch {
        Write-Warning "Could not download voice models automatically ($($_.Exception.Message)). You can add them later."
    }
}

# 5. Build and Launch
Write-Host "Building and launching Hermes container..." -ForegroundColor Cyan
docker compose -f docker-compose.hermes.yml build hermes
docker compose -f docker-compose.hermes.yml up -d hermes

# 6. Verify Health
Write-Host "Waiting for Hermes to be ready on port 8787..." -ForegroundColor Cyan
for ($i = 0; $i -lt 15; $i++) {
    try {
        $res = Invoke-RestMethod -Uri "http://127.0.0.1:8787/healthz" -TimeoutSec 3 -ErrorAction SilentlyContinue
        Write-Host "✓ Hermes is healthy and running!" -ForegroundColor Green
        break
    } catch {
        Start-Sleep -Seconds 2
    }
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " Hermes Assistant is running on your Mini-PC!" -ForegroundColor Green
Write-Host " Access: http://localhost:8787/" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Green
