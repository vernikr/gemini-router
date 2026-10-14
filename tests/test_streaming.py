"""Streaming transport, provisional output, shared accounting and legacy parity."""
import asyncio
import json

import httpx
import pytest

from conftest import BASE, make_config, r429_day
from gemini_router import GeminiClient, Router, RouterError, RouterRequest
from gemini_router.config import key_id

MODEL = 'gemini-3.8-flash'
URL = f'{BASE}/models/{MODEL}:streamGenerateContent'


def event(text='', finish=None, **extra):
    candidate = {'content': {'parts': [{'text': text}]}, 'index': 0}
    if finish:
        candidate['finishReason'] = finish
    return {'candidates': [candidate], **extra}


def sse(*events):
    return httpx.Response(200, text=': heartbeat\r\n\r\n' + ''.join(
        f'data: {json.dumps(e, ensure_ascii=False)}\r\n\r\n' for e in events),
        headers={'content-type': 'text/event-stream'})


async def test_stream_medium_usage_and_cache(tmp_path, mock_api):
    thought = {'candidates': [{'content': {'parts': [{'text': 'hidden', 'thought': True}]}}]}
    route = mock_api.post(URL).mock(return_value=sse(
        thought, event('Привет '), event('世界', 'STOP'),
        {'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 4,
                           'thoughtsTokenCount': 7}}))
    router = Router(make_config(tmp_path))
    events = []
    try:
        result = await router.complete(RouterRequest('hello'), events.append, stream=True)
        assert result.text == 'Привет 世界'
        assert result.tokens_in == 12 and result.tokens_out == 4 and result.tokens_thought == 7
        assert [e.detail['text'] for e in events if e.stage == 'text'] == ['Привет ', '世界']
        assert not any(e.stage == 'text' for e in result.events)
        request = route.calls[0].request
        assert request.url.params['alt'] == 'sse'
        assert json.loads(request.content)['generationConfig']['thinkingConfig'] == {
            'thinkingLevel': 'medium'}
        cached = await router.complete(RouterRequest('hello'), stream=True)
        assert cached.cached and cached.text == result.text and route.call_count == 1
    finally:
        await router.aclose()


async def test_stream_quota_switch(tmp_path, mock_api):
    mock_api.post(URL).mock(return_value=r429_day())
    mock_api.post(f'{BASE}/models/gemini-3.7-flash:streamGenerateContent').mock(
        return_value=sse(event('fallback', 'STOP')))
    router = Router(make_config(tmp_path))
    try:
        result = await router.complete(RouterRequest('hello'), stream=True)
        assert result.model == 'gemini-3.7-flash' and result.text == 'fallback'
    finally:
        await router.aclose()


async def test_interrupted_stream_resets_and_counts_both_attempts(tmp_path, mock_api):
    route = mock_api.post(URL).mock(side_effect=[sse(event('discard')),
                                               sse(event('complete', 'STOP'))])
    router = Router(make_config(tmp_path))
    buffer = []
    starts = []

    def receive(e):
        if e.stage == 'stream_start':
            buffer.clear()
            starts.append(e)
        if e.stage == 'text':
            buffer.append(e.detail['text'])

    try:
        result = await router.complete(RouterRequest('hello'), receive, stream=True)
        assert result.text == ''.join(buffer) == 'complete'
        assert route.call_count == len(starts) == 2
        assert router.ledger.rpd_spent(key_id('k1'), MODEL, 0, 0) == (2, 2)
    finally:
        await router.aclose()


@pytest.mark.parametrize('finish', ['MAX_TOKENS', 'SAFETY', 'RECITATION'])
async def test_incomplete_results_are_not_cached(tmp_path, mock_api, finish):
    mock_api.post(URL).mock(return_value=sse(event('partial', finish)))
    router = Router(make_config(tmp_path, chain=[MODEL]))
    try:
        with pytest.raises(RouterError):
            await router.complete(RouterRequest('hello'), stream=True)
        assert router.ledger._conn.execute('SELECT COUNT(*) FROM cache').fetchone()[0] == 0
    finally:
        await router.aclose()


async def test_auth_fallback_and_multiline_event(tmp_path, respx_mock):
    body = 'data: {"candidates":\ndata: [{"content":{"parts":[{"text":"ok"}]},\n' \
           'data: "finishReason":"STOP"}]}\n\n'
    route = respx_mock.post(URL).mock(side_effect=[
        httpx.Response(401, text='authentication'), httpx.Response(200, text=body)])
    client = GeminiClient(make_config(tmp_path))
    try:
        chunks = []
        result = await client.generate('k1', MODEL, prompt='hello', on_text=chunks.append)
        assert result.text == 'ok' and chunks == ['ok']
        assert route.calls[1].request.url.params['key'] == 'k1'
    finally:
        await client.aclose()


async def test_cancelled_stream_keeps_conservative_reservation(tmp_path, mock_api):
    async def cancel(request):
        raise asyncio.CancelledError

    mock_api.post(URL).mock(side_effect=cancel)
    router = Router(make_config(tmp_path))
    try:
        with pytest.raises(asyncio.CancelledError):
            await router.complete(RouterRequest('hello'), stream=True)
        assert router.ledger.rpd_spent(key_id('k1'), MODEL, 0, 0) == (1, 1)
    finally:
        await router.aclose()


@pytest.mark.parametrize('stage', ['stream_start', 'text'])
async def test_disconnected_consumer_resolves_reservation(tmp_path, respx_mock, stage):
    respx_mock.get(f'{BASE}/models').mock(return_value=httpx.Response(
        200, json={'models': [{'name': f'models/{MODEL}'}]}))
    if stage == 'text':
        respx_mock.post(URL).mock(return_value=sse(event('partial', 'STOP')))
    router = Router(make_config(tmp_path))

    def receive(event):
        if event.stage == stage:
            raise BrokenPipeError

    try:
        with pytest.raises(BrokenPipeError):
            await router.complete(RouterRequest('hello'), receive, stream=True)
        statuses = router.ledger._conn.execute('SELECT status FROM calls').fetchall()
        assert [r[0] for r in statuses] == ['blocked']
    finally:
        await router.aclose()
