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
