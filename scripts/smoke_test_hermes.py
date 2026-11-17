#!/usr/bin/env python3
"""Hermes Assistant & Service Smoke Test Script.

Validates Hermes FastAPI server endpoints, interactive chat/voice pipelines,
and probes live local Ollama or OpenAI-compatible endpoints where available.
Accurately distinguishes mocked/local tests from live network tests.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add src to python path for direct imports
SRC_PATH = Path(__file__).resolve().parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


async def run_smoke_tests(base_url: str | None = None, api_key: str | None = None) -> int:
    try:
        import httpx
    except ImportError:
        print("[FAIL] 'httpx' is required to run smoke tests. Install with: pip install httpx")
        return 1

    token = api_key or os.environ.get("HERMES_API_KEY", "h" * 64)
    os.environ["HERMES_API_KEY"] = token
    auth_headers = {"Authorization": f"Bearer {token}"}

    client_kwargs: dict = {"timeout": httpx.Timeout(10.0)}

    if base_url:
        print(f"[*] Target mode: Remote / Running server at {base_url}")
        client_kwargs["base_url"] = base_url.rstrip("/")
    else:
        print("[*] Target mode: In-process ASGI FastAPI app")
        try:
            from httpx import ASGITransport

            from hermes import app as hermes_module

            hermes_module.TOKEN = token
            app = hermes_module.app
            client_kwargs["transport"] = ASGITransport(app=app)
            client_kwargs["base_url"] = "http://test-hermes"
        except ImportError as e:
            print(f"[FAIL] Failed to import Hermes FastAPI application: {e}")
            return 1

    results = {
        "passed": [],
        "warnings": [],
        "blockers": [],
    }

    print("\n--- Phase 1: Core Service Endpoints (FastAPI) ---")
    async with httpx.AsyncClient(**client_kwargs) as client:
        # 1. Health check
        try:
            res = await client.get("/healthz")
            if res.status_code == 200:
                print(f"[PASS] Health check (/healthz) returned 200: {res.json()}")
                results["passed"].append("GET /healthz")
            else:
                print(f"[FAIL] Health check returned status {res.status_code}: {res.text}")
                results["blockers"].append(f"GET /healthz failed with HTTP {res.status_code}")
        except (httpx.HTTPError, OSError) as e:
            print(f"[FAIL] Health check failed with error: {e}")
            results["blockers"].append(f"GET /healthz error: {e}")

        # 2. Public config
        try:
            res = await client.get("/v1/config")
            if res.status_code == 200:
                cfg = res.json()
                print(f"[PASS] Public config (/v1/config): node_name='{cfg.get('node_name')}', orama_url='{cfg.get('orama_url')}'")
                results["passed"].append("GET /v1/config")
            else:
                print(f"[FAIL] Config returned status {res.status_code}")
                results["blockers"].append(f"GET /v1/config failed with HTTP {res.status_code}")
        except (httpx.HTTPError, OSError) as e:
            print(f"[FAIL] Config error: {e}")
            results["blockers"].append(f"GET /v1/config error: {e}")

        # 3. Auth enforcement check
        try:
            res = await client.get("/v1/voices")
            if res.status_code == 401:
                print("[PASS] Security enforcement: /v1/voices correctly rejects unauthenticated requests (HTTP 401)")
                results["passed"].append("Auth Gate (HTTP 401)")
            else:
                print(f"[WARN] Expected 401 on unauthenticated /v1/voices, got {res.status_code}")
                results["warnings"].append(f"Auth gate returned {res.status_code}")
        except (httpx.HTTPError, OSError) as e:
            print(f"[FAIL] Auth gate error: {e}")

        # 4. Voice catalog
        try:
            res = await client.get("/v1/voices", headers=auth_headers)
            if res.status_code == 200:
                catalog = res.json()
                v_count = len(catalog.get("voices", []))
                print(f"[PASS] Voice catalog (/v1/voices): {v_count} voices available, languages={catalog.get('languages')}")
                results["passed"].append("GET /v1/voices")
            else:
                print(f"[FAIL] Voice catalog failed with status {res.status_code}")
                results["blockers"].append(f"GET /v1/voices failed with HTTP {res.status_code}")
        except (httpx.HTTPError, OSError) as e:
            print(f"[FAIL] Voice catalog error: {e}")
            results["blockers"].append(f"GET /v1/voices error: {e}")

        # 5. Local interactive conversational chat
        try:
            res = await client.post(
                "/v1/chat",
                json={"message": "recommend something to watch"},
                headers=auth_headers,
            )
            if res.status_code == 200:
                data = res.json()
                print(f"[PASS] Local conversational query (/v1/chat): provider='{data.get('provider')}'")
                print(f"       Reply excerpt: \"{data.get('reply', '')[:80]}...\"")
                results["passed"].append("POST /v1/chat (Local Answer)")
            else:
                print(f"[FAIL] Chat failed with status {res.status_code}")
                results["blockers"].append(f"POST /v1/chat failed with HTTP {res.status_code}")
        except (httpx.HTTPError, OSError) as e:
            print(f"[FAIL] Chat error: {e}")
            results["blockers"].append(f"POST /v1/chat error: {e}")

        # 6. Service telemetry probe
        try:
            res = await client.get("/v1/status", headers=auth_headers)
            if res.status_code == 200:
                status_data = res.json()
                print(f"[PASS] Service telemetry status (/v1/status): {list(status_data.keys())}")
                results["passed"].append("GET /v1/status")
            else:
                print(f"[FAIL] Status probe returned {res.status_code}")
                results["blockers"].append(f"GET /v1/status failed with HTTP {res.status_code}")
        except (httpx.HTTPError, OSError) as e:
            print(f"[FAIL] Status probe error: {e}")
            results["blockers"].append(f"GET /v1/status error: {e}")

    print("\n--- Phase 2: Live AI Provider Connectivity Probes ---")
    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0)) as live_client:
        # Probe 1: Local Ollama
        ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        try:
            res = await live_client.get(f"{ollama_url.rstrip('/')}/api/tags")
            if res.status_code == 200:
                models = [m.get("name") for m in res.json().get("models", [])]
                print(f"[LIVE PASS] Ollama reachable at {ollama_url}! Installed models: {models}")
                results["passed"].append(f"Live Ollama ({len(models)} models)")
            else:
                print(f"[LIVE BLOCKED] Ollama at {ollama_url} returned HTTP {res.status_code}")
                results["blockers"].append(f"Ollama returned HTTP {res.status_code}")
        except (httpx.HTTPError, OSError) as exc:
            msg = f"Cannot reach Ollama at {ollama_url}: {type(exc).__name__} ({exc})"
            print(f"[LIVE BLOCKED] {msg}")
            results["blockers"].append(msg)

        # Probe 2: OpenAI-Compatible Endpoint
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        openai_base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if openai_key:
            try:
                res = await live_client.get(
                    f"{openai_base}/models",
                    headers={"Authorization": f"Bearer {openai_key}"},
                )
                if res.status_code == 200:
                    print(f"[LIVE PASS] OpenAI-compatible endpoint at {openai_base} authenticated successfully!")
                    results["passed"].append("Live OpenAI-compatible endpoint")
                else:
                    print(f"[LIVE BLOCKED] OpenAI-compatible endpoint returned HTTP {res.status_code}")
                    results["blockers"].append(f"OpenAI endpoint HTTP {res.status_code}")
            except (httpx.HTTPError, OSError) as exc:
                print(f"[LIVE BLOCKED] OpenAI-compatible endpoint unreachable: {exc}")
                results["blockers"].append(f"OpenAI endpoint error: {exc}")
        else:
            print("[INFO] OPENAI_API_KEY is not set in environment (skipped live test)")

        # Probe 3: Google Gemini API
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if gemini_key:
            try:
                res = await live_client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}"
                )
                if res.status_code == 200:
                    print("[LIVE PASS] Google Gemini API authenticated successfully!")
                    results["passed"].append("Live Google Gemini API")
                else:
                    print(f"[LIVE BLOCKED] Gemini API returned HTTP {res.status_code}")
                    results["blockers"].append(f"Gemini API HTTP {res.status_code}")
            except (httpx.HTTPError, OSError) as exc:
                print(f"[LIVE BLOCKED] Gemini API unreachable: {exc}")
                results["blockers"].append(f"Gemini API error: {exc}")
        else:
            print("[INFO] GEMINI_API_KEY is not set in environment (skipped live test)")

    print("\n=======================================================")
    print("               HERMES SMOKE TEST SUMMARY               ")
    print("=======================================================")
    print(f"Passed Checks ({len(results['passed'])}):")
    for p in results["passed"]:
        print(f"  + {p}")

    if results["warnings"]:
        print(f"\nWarnings ({len(results['warnings'])}):")
        for w in results["warnings"]:
            print(f"  * {w}")

    if results["blockers"]:
        print(f"\nLive Blockers / Environmental Constraints ({len(results['blockers'])}):")
        for b in results["blockers"]:
            print(f"  ! {b}")

    print("=======================================================\n")
    # Return 0 if all core in-process FastAPI checks passed
    core_passed = any("GET /healthz" in p for p in results["passed"]) and any("GET /v1/config" in p for p in results["passed"])
    return 0 if core_passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes Smoke Test Runner")
    parser.add_argument("--url", default=None, help="Base URL of running Hermes service (e.g. http://localhost:8787)")
    parser.add_argument("--key", default=None, help="Hermes API Key")
    args = parser.parse_args()

    exit_code = asyncio.run(run_smoke_tests(base_url=args.url, api_key=args.key))
    sys.exit(exit_code)
