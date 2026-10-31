"""主审回归：日志导出内存有界，脱敏异常禁止回传原始秘密。"""
import io

import asyncio
import httpx
from test_diag_export_health import webui


def test_http_redirect_or_html_is_not_healthy(monkeypatch):
    async def run():
        for response in (httpx.Response(302, headers={'Location': '/login'}),
                         httpx.Response(200, text='<html>login</html>')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: response)) as client:
                monkeypatch.setattr(webui, 'get_http', lambda _req: client)
                result = await webui._probe_http_layer(None, 'http://internal/healthz')
                assert result['ok'] is False
    asyncio.run(run())


def test_status_does_not_claim_actual_playback_from_ready_script(monkeypatch):
    async def run():
        monkeypatch.setattr(webui, 'supervisor_status', lambda **_kw: {
            'lxmusic': {'state': 'RUNNING'}, 'lxserver': {'state': 'RUNNING'}})
        async def ready(*_args, **_kwargs):
            return {'ok': True, 'data': {'user_source': {'initialized': True, 'state': 'ready'}}}
        monkeypatch.setattr(webui, '_probe_http_layer', ready)
        result = await webui._layered_health(None, 'lxmusic')
        assert result['playback']['ok'] is None
        assert result['playback']['state'] == 'not_probed'
    asyncio.run(run())


def test_redaction_error_fails_closed(monkeypatch):
    def broken(_text):
        raise ValueError('broken redactor')
    monkeypatch.setattr(webui, 'redact', broken)
    assert b'secret-value' not in webui._redact_chunk(b'token=secret-value')


def test_oversized_line_is_not_buffered_or_split_into_raw_tokens(monkeypatch):
    sizes = []
    real = webui.redact
    def measured(text):
        sizes.append(len(text))
        return real(text)
    monkeypatch.setattr(webui, 'redact', measured)
    source = io.BytesIO(b'prefix token=' + b'x' * 200000 + b'\nnormal line\n')
    output = io.BytesIO()
    webui._copy_redacted(source, output)
    assert max(sizes, default=0) <= 65536
    assert b'normal line' in output.getvalue()
    assert b'x' * 100 not in output.getvalue()


def test_log_snapshot_read_has_total_limit():
    # 即使日志在导出期间持续增长，也不无限读取。
    output = io.BytesIO()
    webui._copy_redacted(io.BytesIO(b'line\n' * 100), output, limit_bytes=20)
    assert output.getvalue().count(b'line\n') <= 4
    assert b'truncated' in output.getvalue()
