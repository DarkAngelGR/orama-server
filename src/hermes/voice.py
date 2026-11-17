"""Gemini neural speech with an interruptible, CPU-only Piper fallback."""

import asyncio
import base64
import importlib.util
import io
import json
import os
import sys
import wave
from pathlib import Path
from typing import Literal

try:
    import httpx
except ImportError:
    httpx = None

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .conversation import FAILURES, gemini_model
from .streams import json_lines

VOICES = ['Kore','Puck','Charon','Fenrir','Aoede']
LOCAL_VOICES = {
    'en_US-lessac-medium': ('en-US','Lessac'),
    'en_US-ryan-medium': ('en-US','Ryan'),
    'el_GR-rapunzelina-low': ('el-GR','Rapunzelina'),
}
MAX_AUDIO = 16 * 1024 * 1024


class VoiceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=2000)
    language: Literal['en-US','el-GR'] = 'en-US'
    voice: Literal['Kore','Puck','Charon','Fenrir','Aoede'] = 'Kore'
    provider: Literal['auto','gemini','piper'] = 'auto'
    local_voice: Literal['auto','en_US-lessac-medium','en_US-ryan-medium','el_GR-rapunzelina-low'] = 'auto'

    @field_validator('text')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Speech cannot be blank')
        return value.strip()


def model_dir():
    return Path(os.environ.get('PIPER_MODEL_DIR','/voices'))


def voice_catalog():
    installed = importlib.util.find_spec('piper') is not None
    return {'languages':['en-US','el-GR'], 'voices':[{'id':v,'provider':'gemini'} for v in VOICES],
            'gemini_configured':bool(os.environ.get('GEMINI_API_KEY')),
            'tts_model':os.environ.get('GEMINI_TTS_MODEL','gemini-3.8-flash-tts'),
            'local_voices':[{'id':v,'language':lang,'name':name,
                             'available': installed and (model_dir()/f'{v}.onnx').is_file()
                             and (model_dir()/f'{v}.onnx.json').is_file()}
                            for v,(lang,name) in LOCAL_VOICES.items()]}


def wav_bytes(pcm, rate=24000):
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_AUDIO:
        raise ValueError('Invalid PCM')
    out = io.BytesIO()
    with wave.open(out,'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue()


def audio_event(data, provider):
    if len(data) > MAX_AUDIO:
        raise ValueError('Audio limit')
    return {'type':'audio','provider':provider,'mime':'audio/wav',
            'data':base64.b64encode(data).decode('ascii')}


async def gemini_audio(req, client):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise ValueError('Gemini not configured')
    model = gemini_model('GEMINI_TTS_MODEL','gemini-3.8-flash-tts')
    modern = model.startswith('gemini-3.8-')
    language = 'Greek' if req.language == 'el-GR' else 'English'
    if modern:
        url = 'https://generativelanguage.googleapis.com/v1beta/interactions'
        body = {'model':model,'input':[{'type':'user_input','content':[{'type':'text','text':req.text,
                 'annotations':[{'type':'speech_metadata','style':f'Warm, conversational {language}.'}]}]}],
                'response_format':{'type':'audio'},'generation_config':{'speech_config':[{'voice':req.voice}]},'stream':True}
    else:
        # The generateContent adapter also supports installations on 2.5 / 3.1 TTS.
        streaming = not model.startswith('gemini-2.5-')
        method = 'streamGenerateContent?alt=sse' if streaming else 'generateContent'
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}'
        body = {'contents':[{'parts':[{'text':f'Read aloud naturally in {language}:\n{req.text}'}]}],
                'generationConfig':{'responseModalities':['AUDIO'],
                    'speechConfig':{'voiceConfig':{'prebuiltVoiceConfig':{'voiceName':req.voice}}}}}
    total = 0
    completed = False
    async with asyncio.timeout(45):
        async with client.stream('POST',url,headers={'x-goog-api-key':key},json=body,
                                 timeout=httpx.Timeout(25,connect=5),follow_redirects=False) as res:
            res.raise_for_status()
            if not modern and model.startswith('gemini-2.5-'):
                data = bytearray()
                async for chunk in res.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_AUDIO:
                        raise ValueError('Audio limit')
                objects = [json.loads(data)]
                async def unary():
                    for item in objects:
                        yield item
                events = unary()
            else:
                events = json_lines(res,sse=True,limit=MAX_AUDIO,total_limit=24_000_000)
            async for event in events:
                if modern:
                    if event.get('event_type') == 'interaction.complete':
                        completed = True
                    delta = event.get('delta',{})
                    chunks = [delta] if delta.get('type') == 'audio' else []
                else:
                    chunks = []
                    for candidate in event.get('candidates',[]):
                        if candidate.get('finishReason') == 'STOP':
                            completed = True
                        for part in candidate.get('content',{}).get('parts',[]):
                            if 'inlineData' in part:
                                chunks.append(part['inlineData'])
                for chunk in chunks:
                    raw = base64.b64decode(chunk.get('data',''),validate=True)
                    total += len(raw)
                    if total > MAX_AUDIO:
                        raise ValueError('Audio limit')
                    if not modern:
                        mime = chunk.get('mimeType','')
                        if 'audio/L16' not in mime and 'audio/l16' not in mime and 'audio/pcm' not in mime:
                            raise ValueError('Unsupported audio format')
                    yield wav_bytes(raw)
    if not total or not completed:
        raise ValueError('Incomplete audio response')


async def local_audio(req):
    voice_id = req.local_voice
    if voice_id == 'auto':
        voice_id = 'el_GR-rapunzelina-low' if req.language == 'el-GR' else 'en_US-lessac-medium'
    if LOCAL_VOICES[voice_id][0] != req.language:
        raise ValueError('Voice language mismatch')
    model = model_dir() / f'{voice_id}.onnx'
    if not model.is_file() or not model.with_suffix('.onnx.json').is_file():
        raise ValueError('Local voice not installed')
    # Separate process permits hard cancellation and bounds native inference lifetime.
    proc = await asyncio.create_subprocess_exec(sys.executable,'-m','jarvis.piper_worker',str(model),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ,'OMP_NUM_THREADS':'2'})
    try:
        async with asyncio.timeout(30):
            proc.stdin.write(req.text.encode('utf-8'))
            await proc.stdin.drain()
            proc.stdin.close()
            data = bytearray()
            while chunk := await proc.stdout.read(65536):
                data.extend(chunk)
                if len(data) > MAX_AUDIO:
                    raise ValueError('Audio limit')
            await proc.wait()
        if proc.returncode or not data.startswith(b'RIFF'):
            raise ValueError('Local synthesis failed')
        return bytes(data)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def stream_voice(req, client):
    providers = ['gemini','piper'] if req.provider == 'auto' else [req.provider]
    for provider in providers:
        started = False
        try:
            if provider == 'gemini':
                async for data in gemini_audio(req,client):
                    started = True
                    yield audio_event(data,provider)
            else:
                yield audio_event(await local_audio(req),provider)
            yield {'type':'done','provider':provider}
            return
        except FAILURES + (OSError,):
            if started:
                # Replaying the whole utterance after partial speech is confusing.
                yield {'type':'error','message':'Speech was interrupted upstream. The text is still available; try replay or local voice.'}
                return
            yield {'type':'warning','message':f'{provider.capitalize()} voice is unavailable or limited.'}
    yield {'type':'error','message':'Voice is unavailable. Check your provider or install a local voice. Your text conversation still works.'}
