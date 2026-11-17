"""Hermes assistant integration for Orama.

Serves the Hermes conversational voice & media intelligence dashboard under /hermes,
sharing Orama's single server, port, session authentication, and Design D aesthetics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, Response, g, jsonify, redirect, request, send_from_directory

from orama.web import auth, security

try:
    import httpx

    from hermes.conversation import ChatRequest, stream_chat
    from hermes.recommend import reorder
    from hermes.status import service_status
    from hermes.streams import encode_events
    from hermes.voice import VoiceRequest, stream_voice, voice_catalog

    HAS_HERMES_BACKEND = True
except ImportError:
    httpx = None
    ChatRequest = None
    stream_chat = None
    reorder = None
    service_status = None
    encode_events = None
    VoiceRequest = None
    stream_voice = None
    voice_catalog = None
    HAS_HERMES_BACKEND = False

LOG = logging.getLogger("orama.hermes")
HERMES_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "hermes" / "static"
HERMES_DATA_DIR = Path(os.environ.get("HERMES_DATA_DIR", os.environ.get("JARVIS_DATA_DIR", "/data")))
CACHE_FILE = HERMES_DATA_DIR / "recommendations.json"


def _authenticate() -> bool:
    """True when request carries an active Orama session or a valid Hermes API key."""
    if g.get("principal"):
        return True
    auth_hdr = request.headers.get("Authorization", "")
    token = os.environ.get("HERMES_API_KEY", os.environ.get("JARVIS_API_KEY", ""))
    return bool(token and len(token) >= 32 and secrets.compare_digest(auth_hdr, f"Bearer {token}"))


def _read_cache() -> dict[str, Any]:
    try:
        doc = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("recommendations"), list):
            return doc
    except (OSError, ValueError):
        pass
    return {"updated_at": None, "recommendations": [], "status": "not_synced"}


def sync_stream(async_gen):
    """Bridge an async generator to a synchronous generator for WSGI."""
    loop = asyncio.new_event_loop()
    try:
        ait = async_gen.__aiter__()
        while True:
            try:
                item = loop.run_until_complete(ait.__anext__())
                yield item.encode("utf-8") if isinstance(item, str) else item
            except StopAsyncIteration:
                break
    finally:
        loop.close()


async def chat_events(req):
    if not HAS_HERMES_BACKEND or httpx is None:
        return
    async with httpx.AsyncClient(trust_env=False) as client:
        async for event in stream_chat(req, client):
            yield event


async def voice_events(req):
    if not HAS_HERMES_BACKEND or httpx is None:
        return
    async with httpx.AsyncClient(trust_env=False) as client:
        async for event in stream_voice(req, client):
            yield event


def register_hermes(app: Flask) -> None:
    app.register_blueprint(_pages())


def _pages() -> Blueprint:
    pages = Blueprint("hermes", __name__, url_prefix="/hermes")

    @pages.get("")
    def hermes_redirect():
        external_url = os.environ.get("HERMES_URL", "").rstrip("/")
        if external_url:
            return redirect(external_url, code=302)
        return redirect("/hermes/", code=308)

    @pages.get("/")
    @auth.requires("apps")
    def hermes_index():
        external_url = os.environ.get("HERMES_URL", "").rstrip("/")
        if external_url:
            return redirect(external_url, code=302)
        index_file = HERMES_STATIC_DIR / "index.html"
        content = index_file.read_text(encoding="utf-8")
        # Inject Orama session context and CSRF token so Hermes connects seamlessly
        session_meta = (
            f'<meta name="csrf-token" content="{security.csrf_token()}">\n'
            f'<meta name="orama-session" content="{g.principal}">\n'
        )
        content = content.replace("<head>", f"<head>\n  {session_meta}", 1)
        return Response(content, mimetype="text/html")

    @pages.get("/static/<path:filename>")
    def hermes_static(filename: str):
        return send_from_directory(HERMES_STATIC_DIR, filename)

    @pages.get("/v1/config")
    def config():
        return jsonify({
            "orama_url": "",
            "node_name": os.environ.get("NODE_NAME", "Orama Embedded Node"),
        })

    @pages.get("/v1/status")
    def status():
        if not _authenticate():
            return jsonify({"error": "Unauthorized"}), 401
        if not HAS_HERMES_BACKEND or httpx is None:
            return jsonify({"services": {}, "models": {}}), 503

        async def _run():
            async with httpx.AsyncClient(trust_env=False) as client:
                return await service_status(client)

        try:
            return jsonify(asyncio.run(_run()))
        except (OSError, RuntimeError, TimeoutError) as err:
            LOG.warning("Status check failed: %s", err)
            return jsonify({"services": {}, "models": {}}), 500

    @pages.get("/v1/voices")
    def voices():
        if not _authenticate():
            return jsonify({"error": "Unauthorized"}), 401
        if not HAS_HERMES_BACKEND:
            return jsonify({"gemini_voices": [], "local_voices": []})
        return jsonify(voice_catalog())

    @pages.get("/v1/recommendations")
    def recommendations():
        if not _authenticate():
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify(_read_cache())

    @pages.post("/v1/refresh")
    def refresh():
        if not _authenticate():
            return jsonify({"error": "Unauthorized"}), 401
        if not HAS_HERMES_BACKEND or httpx is None:
            return jsonify({"error": "Hermes backend dependencies not installed"}), 503

        async def _run():
            async with httpx.AsyncClient(trust_env=False) as client:
                from hermes.app import gemini, ollama
                return await reorder(client, gemini, ollama)

        try:
            res = asyncio.run(_run())
            return jsonify(res)
        except (OSError, RuntimeError, ValueError, TimeoutError) as err:
            LOG.warning("Recommendations refresh failed: %s", err)
            return jsonify({"error": str(err)}), 500

    @pages.post("/v1/chat/stream")
    def chat():
        if not _authenticate():
            return jsonify({"error": "Unauthorized"}), 401
        if not HAS_HERMES_BACKEND:
            return jsonify({"error": "Hermes backend dependencies not installed"}), 503
        payload = request.get_json(silent=True) or {}
        try:
            req = ChatRequest(**payload)
        except (TypeError, ValueError) as err:
            return jsonify({"error": str(err)}), 422

        return Response(
            sync_stream(encode_events(chat_events(req))),
            mimetype="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @pages.post("/v1/tts/stream")
    def tts():
        if not _authenticate():
            return jsonify({"error": "Unauthorized"}), 401
        if not HAS_HERMES_BACKEND:
            return jsonify({"error": "Hermes backend dependencies not installed"}), 503
        payload = request.get_json(silent=True) or {}
        try:
            req = VoiceRequest(**payload)
        except (TypeError, ValueError) as err:
            return jsonify({"error": str(err)}), 422

        return Response(
            sync_stream(encode_events(voice_events(req))),
            mimetype="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return pages
