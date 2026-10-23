import socket

import pytest

from proxy.media_url import validate_media_url


def resolver(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://media.example.com/a.mp3", "scheme_not_https"),
        ("https://user:pass@media.example.com/a.mp3", "userinfo_present"),
        ("https://media.example.com:0/a.mp3", "invalid_port"),
        ("https://127.0.0.1/a.mp3", "host_not_public"),
        ("https://[::1]/a.mp3", "host_not_public"),
        ("https://169.254.169.254/a.mp3", "host_not_public"),
        ("https://[::ffff:127.0.0.1]/a.mp3", "host_not_public"),
        ("https://[fe80::1]/a.mp3", "host_not_public"),
        ("file:///music/a.mp3", "invalid_scheme"),
        ("https://media.example.com/a\r\nLocation:evil", "control_characters"),
        ("https://media.example.com\\@127.0.0.1/a", "ambiguous_url"),
    ],
)
def test_rejects_unsafe_targets(url, reason):
    decision = validate_media_url(url)
    assert not decision.ok
    assert decision.reason == reason


def test_rejects_private_dns_answer():
    decision = validate_media_url("https://media.example.com/a.flac", resolver=lambda *_: resolver("10.0.0.8"))
    assert decision.reason == "resolved_address_not_public"


def test_accepts_https_public_dns_answers():
    decision = validate_media_url(
        "https://media.example.com/a.flac?sig=x",
        resolver=lambda *_: resolver("93.184.216.34") + resolver("2606:2800:220:1:248:1893:25c8:1946"),
    )
    assert decision.ok
    assert decision.host == "media.example.com"


def test_mixed_public_and_private_dns_answers_are_rejected():
    decision = validate_media_url(
        "https://media.example.com/a.flac",
        resolver=lambda *_: resolver("93.184.216.34") + resolver("192.168.1.1"),
    )
    assert decision.reason == "resolved_address_not_public"
