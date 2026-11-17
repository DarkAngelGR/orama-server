"""Conversational provider adapters; no tools, credentials or network addresses in prompts."""
import asyncio
import os
import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ConfigDict, field_validator

from .providers import private_server_url
from .streams import json_lines

SYSTEM = ('You are Hermes, a warm, concise personal media assistant for Orama. Reply in the user\'s language. '
          'You cannot execute commands, access private files, or control devices. '
          'Never claim to have performed actions. Keep spoken answers natural and brief.')
FAILURES = (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError, TimeoutError)


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=2000)
    history: list[Message] = Field(default_factory=list, max_length=20)
    provider: Literal['auto', 'gemini', 'ollama'] = 'auto'

    @field_validator('message')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Message cannot be blank')
        return value.strip()

    @field_validator('history')
    @classmethod
    def bounded_history(cls, value):
        if sum(len(m.content) for m in value) > 32000:
            raise ValueError('History too long')
        return value


def gemini_model(variable='GEMINI_MODEL', default='gemini-2.5-flash'):
    model = os.environ.get(variable, default)
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,80}', model):
        raise ValueError('Invalid model configuration')
    return model


async def provider_tokens(req, client, provider):
    history = [m.model_dump() for m in req.history] + [{'role':'user','content':req.message}]
    if provider == 'gemini':
        key = os.environ.get('GEMINI_API_KEY')
        if not key:
            raise ValueError('Not configured')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:streamGenerateContent?alt=sse'
        data = {'systemInstruction': {'parts':[{'text':SYSTEM}]},
                'contents':[{'role':'model' if m['role']=='assistant' else 'user',
                             'parts':[{'text':m['content']}]} for m in history],
                'generationConfig':{'temperature':0.5,'maxOutputTokens':2048}}
        headers = {'x-goog-api-key':key}
    else:
        url = private_server_url(os.environ.get('OLLAMA_URL','http://ollama:11434')) + '/api/chat'
        data = {'model':os.environ.get('OLLAMA_MODEL','llama3.1:8b'),
                'messages':[{'role':'system','content':SYSTEM}]+history,
                'stream':True,'options':{'num_predict':700}}
        headers = {}
    completed = False
    async with asyncio.timeout(100 if provider == 'ollama' else 40):
        async with client.stream('POST', url, json=data, headers=headers,
                                 timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as res:
            res.raise_for_status()
            async for obj in json_lines(res, sse=provider=='gemini'):
                if provider == 'gemini':
                    for candidate in obj.get('candidates', []):
                        if candidate.get('finishReason'):
                            if candidate['finishReason'] not in ('STOP','MAX_TOKENS'):
                                raise ValueError('Blocked generation')
                            completed = True
                        for part in candidate.get('content',{}).get('parts',[]):
                            if not part.get('thought') and isinstance(part.get('text'),str):
                                yield part['text']
                else:
                    text = obj.get('message',{}).get('content','')
                    if not isinstance(text,str):
                        raise ValueError('Invalid response')
                    if text:
                        yield text
                    if obj.get('done') is True:
                        completed = True
    if not completed:
        raise ValueError('Incomplete stream')


async def stream_chat(req, client):
    choices = ['gemini','ollama'] if req.provider == 'auto' else [req.provider]
    for provider in choices:
        count = 0
        yield {'type':'provider','provider':provider}
        try:
            async for token in provider_tokens(req, client, provider):
                token = token[:8000-count]
                if token:
                    count += len(token)
                    yield {'type':'delta','text':token}
                if count >= 8000:
                    break
            if not count:
                raise ValueError('Empty response')
            yield {'type':'done','provider':provider}
            return
        except FAILURES:
            if count:
                yield {'type':'reset'}
            yield {'type':'warning','message':f'{provider.capitalize()} is unavailable or limited.'}
    yield {'type':'error','message':'The selected conversation providers are unavailable. Please try again shortly.'}
