"""Bounded upstream parsing and a small, provider-neutral event protocol."""
import json


async def json_lines(response, *, sse=False, limit=2_000_000, total_limit=24_000_000):
    pending = bytearray()
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > total_limit:
            raise ValueError('Upstream response limit')
        pending.extend(chunk)
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending = bytearray(rest)
            if len(line) > limit:
                raise ValueError('Upstream line limit')
            item = parse_line(line, sse)
            if item is not None:
                yield item
        if len(pending) > limit:
            raise ValueError('Upstream line limit')
    if pending.strip():
        item = parse_line(pending, sse)
        if item is not None:
            yield item


def parse_line(line, sse):
    line = bytes(line).strip()
    if sse:
        if not line.startswith(b'data:'):
            return None
        line = line[5:].strip()
    if not line or line == b'[DONE]':
        return None
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError('Invalid upstream object')
    if obj.get('error') or obj.get('event_type') == 'error':
        raise ValueError('Upstream error event')
    return obj


async def encode_events(events):
    async for event in events:
        yield json.dumps(event, ensure_ascii=False) + '\n'
