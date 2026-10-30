import socket

import pytest

# 导入待测的 URL 验证函数 validate_media_url 与主机名脱敏函数 hostname_for_log（属于 proxy.media_url 模块）
from proxy.media_url import hostname_for_log, validate_media_url


def resolver(address):
    # 模拟 DNS 解析返回的记录元组结构，支持 IPv4 与 IPv6 地址
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://media.example.com/a.mp3", "scheme_not_https"),
        ("https://user:pass@media.example.com/a.mp3", "userinfo_present"),
        ("https://media.example.com:0/a.mp3", "invalid_port"),
        ("https://127.0.0.1/a.mp3", "host_not_public"),
        ("https://[::1]/a.mp3", "host_not_public"),
        ("https://169.254.169.254/a.mp3", "host_not_public"),
        ("https://100.64.0.1/a.mp3", "host_not_public"),
        ("https://[::ffff:127.0.0.1]/a.mp3", "host_not_public"),
        ("https://[fe80::1]/a.mp3", "host_not_public"),
        ("https://[fd00::1]/a.mp3", "host_not_public"),
        ("https://[2001:db8::1]/a.mp3", "host_not_public"),
        ("https://[fe80::1%25eth0]/a.mp3", "host_not_public"),
        ("file:///music/a.mp3", "invalid_scheme"),
        ("https://media.example.com/a\r\nLocation:evil", "control_characters"),
        ("https://media.example.com\\@127.0.0.1/a", "ambiguous_url"),
    ],
)
def test_rejects_unsafe_targets(url, reason):
    # 验证不安全的目标地址必须被拦截，包括非 HTTPS 协议、私网 IPv4 与私网 IPv6
    decision = validate_media_url(url)
    assert not decision.ok
    assert decision.reason == reason


def test_rejects_private_dns_answer():
    # 验证 DNS 解析结果包含私网 IPv4 地址时必须拒绝交付
    decision = validate_media_url("https://media.example.com/a.flac", resolver=lambda *_: resolver("10.0.0.8"))
    assert decision.reason == "resolved_address_not_public"


def test_rejects_private_ipv6_dns_answer():
    # 验证 DNS 解析结果包含私网 IPv6 ULA 地址时必须拒绝交付
    decision = validate_media_url("https://media.example.com/a.flac", resolver=lambda *_: resolver("fd00::1"))
    assert decision.reason == "resolved_address_not_public"


def test_accepts_https_public_dns_answers():
    # 验证公网 IPv4 与公网 IPv6 双栈解析结果均正常放行
    decision = validate_media_url(
        "https://media.example.com/a.flac?sig=x",
        resolver=lambda *_: resolver("93.184.216.34") + resolver("2606:2800:220:1:248:1893:25c8:1946"),
    )
    assert decision.ok
    assert decision.host == "media.example.com"


def test_accepts_public_ipv6_literal_with_port():
    # 验证公网 IPv6 字面量地址配合自定义 HTTPS 端口能够正确通过校验
    decision = validate_media_url(
        "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/a.flac",
        resolver=lambda *_: resolver("2606:2800:220:1:248:1893:25c8:1946"),
    )
    assert decision.ok
    assert decision.host == "2606:2800:220:1:248:1893:25c8:1946"


def test_mixed_public_and_private_dns_answers_are_rejected():
    # 验证双栈解析只要混入私网地址（如公网 IPv4 混入局域网私网），立刻拒绝
    decision = validate_media_url(
        "https://media.example.com/a.flac",
        resolver=lambda *_: resolver("93.184.216.34") + resolver("192.168.1.1"),
    )
    assert decision.reason == "resolved_address_not_public"


def test_mixed_public_ipv4_and_private_ipv6_dns_answers_are_rejected():
    # 验证公网 IPv4 与私网 IPv6 混合解析同样立刻拒绝
    decision = validate_media_url(
        "https://media.example.com/a.flac",
        resolver=lambda *_: resolver("93.184.216.34") + resolver("fd00::dead:beef"),
    )
    assert decision.reason == "resolved_address_not_public"


def test_hostname_for_log_never_leaks_secrets():
    # 验证日志脱敏提取只保留主机名，绝不泄露凭据 token、签名参数和具体路径
    url_with_token = "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/audio.flac?token=secret_abc123&user=admin"
    assert hostname_for_log(url_with_token) == "2606:2800:220:1:248:1893:25c8:1946"
    assert "token" not in hostname_for_log(url_with_token)
    assert "secret" not in hostname_for_log(url_with_token)

    cdn_url = "https://m801.music.126.net:443/data/stream.mp3?wsSecret=abcdef123456"
    assert hostname_for_log(cdn_url) == "m801.music.126.net"
    assert "wsSecret" not in hostname_for_log(cdn_url)
