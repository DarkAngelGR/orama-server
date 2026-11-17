"""Read-only, bounded connection checks. Never expose configured URLs or credentials."""

import asyncio
import os
import time

try:
    import httpx
except ImportError:
    httpx = None

from .providers import private_server_url

_cache = None
_checked = 0
_lock = asyncio.Lock()


async def probe(client, base, path, headers=None):
    if not base or httpx is None:
        return 'not_configured'
    try:
        async with client.stream('GET',private_server_url(base)+path,headers=headers or {},
                                 timeout=httpx.Timeout(4),follow_redirects=False) as res:
            return 'online' if 200 <= res.status_code < 300 else 'unavailable'
    except (httpx.HTTPError, ValueError):
        return 'unavailable'


async def service_status(client):
    global _cache, _checked
    async with _lock:
        if _cache and time.monotonic()-_checked < 20:
            return _cache
        plex = os.environ.get('PLEX_URL','') if os.environ.get('PLEX_TOKEN') else ''
        jelly = os.environ.get('JELLYFIN_URL','') if os.environ.get('JELLYFIN_API_KEY') else ''
        orama_url = os.environ.get('ORAMA_URL', '')
        ollama, plex_state, jelly_state, nas, orama_state = await asyncio.gather(
            probe(client,os.environ.get('OLLAMA_URL','http://ollama:11434'),'/api/tags'),
            probe(client,plex,'/identity',{'X-Plex-Token':os.environ.get('PLEX_TOKEN','')}),
            probe(client,jelly,'/System/Info',{'X-Emby-Token':os.environ.get('JELLYFIN_API_KEY','')}),
            probe(client,os.environ.get('NAS_HEALTH_URL',''),'/'),
            probe(client,orama_url,'/healthz'))
        openai_key = os.environ.get('OPENAI_API_KEY', '')
        openai_base = os.environ.get('OPENAI_BASE_URL', '')
        openai_status = 'configured' if (openai_key or openai_base) else 'not_configured'
        _cache = {'checked_at':time.time(), 'services':{
            'orama':{'status':orama_state,'detail':'Orama server media request and management system.'},
            'gemini':{'status':'configured' if os.environ.get('GEMINI_API_KEY') else 'not_configured',
                      'detail':'Availability and quota are checked when you send a message.'},
            'openai':{'status':openai_status,
                      'detail':'OpenAI-compatible LLM endpoint (LM Studio, vLLM, OpenRouter, Groq, or OpenAI).'},
            'ollama':{'status':ollama,'detail':'Service reachability; the configured model must also be installed.'},
            'plex':{'status':plex_state},'jellyfin':{'status':jelly_state},
            'nas':{'status':nas,'detail':'HTTP endpoint reachability only; not disk or RAID health.'}},
            'models':{'gemini':os.environ.get('GEMINI_MODEL','gemini-2.5-flash'),
                      'openai':os.environ.get('OPENAI_MODEL','gpt-4o-mini'),
                      'ollama':os.environ.get('OLLAMA_MODEL','llama3.1:8b')}}
        _checked = time.monotonic()
        return _cache
