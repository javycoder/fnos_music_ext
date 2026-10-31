"""曲库扫描配置只允许内部路径与有界JSON对象，补齐PR审查安全边界。"""
import pytest

from proxy import app as proxy_app


@pytest.mark.parametrize('body', ['[]', '1', 'null', '"text"', 'true'])
def test_scan_body_must_be_json_object(monkeypatch, body):
    monkeypatch.setitem(proxy_app.CONF, 'library_scan_path', '/music/api/v1/scan|' + body)
    assert proxy_app._scan_config() == ('', '')


def test_scan_body_is_bounded_before_sending(monkeypatch):
    monkeypatch.setitem(proxy_app.CONF, 'library_scan_path', '/music/api/v1/scan|{"value":"' + 'a' * 65536 + '"}')
    assert proxy_app._scan_config() == ('', '')


@pytest.mark.parametrize('path', ['/\x7fscan', '/%2foutside/scan', '/music/%2e%2e/scan',
                                 '/music/%252e%252e/scan', '/music\\outside/scan', '/music/scan#fragment'])
def test_scan_path_rejects_encoded_ambiguity(monkeypatch, path):
    monkeypatch.setitem(proxy_app.CONF, 'library_scan_path', path)
    assert proxy_app._scan_config() == ('', '')


def test_scan_helper_insertion_preserves_existing_numeric_overflow_guard():
    # PR插入新函数时不能把原推荐配置的OverflowError分支移到新函数下。
    assert proxy_app._safe_liked_number(10 ** 1000, 20, integer=True) == 20
    assert proxy_app._safe_scan_number(10 ** 1000, 3.0, 0, 600) == 3.0


def test_scan_json_object_keeps_unicode_and_relative_path(monkeypatch):
    monkeypatch.setitem(proxy_app.CONF, 'library_scan_path', '/music/api/v1/scan|{"name":"曲库"}')
    assert proxy_app._scan_config() == ('/music/api/v1/scan', '{"name":"曲库"}')


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'<html>login</html>', b'{}', b'[]', b'{"code":false}',
                                  b'{"code":-1,"token":"fixture-secret-value"}'])
async def test_scan_http_200_is_not_business_success_without_valid_code(monkeypatch, caplog, body):
    import httpx
    monkeypatch.setitem(proxy_app.CONF, 'library_scan_path', '/music/api/v1/scan')
    monkeypatch.setattr(proxy_app, '_scan_pending_headers', {'Cookie': 'fixture'})
    monkeypatch.setattr(proxy_app, '_scan_last_attempt', 0)
    caplog.set_level('INFO', logger='fnmusic_proxy')
    response = httpx.Response(200, content=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: response),
                                 base_url='http://official.test') as client:
        monkeypatch.setattr(proxy_app, 'get_upstream_client', lambda _app: client)
        await proxy_app._run_scan_worker(0)
    assert 'Library scan triggered' not in caplog.text
    assert 'fixture-secret-value' not in caplog.text
    assert response.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [0, 200])
async def test_scan_accepts_explicit_official_success_codes(code):
    import httpx
    response = httpx.Response(200, json={'code': code})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: response),
                                 base_url='http://official.test') as client:
        result = await proxy_app._scan_request_outcome(client, client.build_request('POST', '/scan'))
    assert result == 'ok'
    assert response.is_closed


@pytest.mark.asyncio
async def test_scan_response_is_streamed_bounded_and_closed(monkeypatch, caplog):
    import httpx
    received = []
    closed = []
    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _index in range(8):
                received.append(True)
                yield b'x' * 32768
        async def aclose(self):
            closed.append(True)
    monkeypatch.setitem(proxy_app.CONF, 'library_scan_path', '/music/api/v1/scan')
    monkeypatch.setattr(proxy_app, '_scan_pending_headers', {'Cookie': 'fixture'})
    monkeypatch.setattr(proxy_app, '_scan_last_attempt', 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, stream=Oversized())), base_url='http://official.test') as client:
        monkeypatch.setattr(proxy_app, 'get_upstream_client', lambda _app: client)
        await proxy_app._run_scan_worker(0)
    assert len(received) <= 3
    assert closed
    assert 'Library scan triggered' not in caplog.text
