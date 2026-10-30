"""外部直连媒体 URL 的安全校验与主机名脱敏模块。

本模块属于 proxy 目录。主要提供两个功能：
1. validate_media_url：在将 302 重定向交付给客户端前，严格检查目标 URL 是否为公开合规的 HTTPS 地址，拦截私网 IPv4 与私网 IPv6，防止服务端请求伪造（SSRF）；
2. hostname_for_log：从媒体 URL 中仅提取纯净主机名，剥离查询参数与用户凭据，确保运行日志绝不泄露安全敏感信息。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class MediaURLDecision:
    """媒体 URL 安全校验结果。"""
    ok: bool  # 是否允许向外部客户端交付
    reason: str = ""  # 校验失败的具体原因代码
    host: str = ""  # 校验通过或触发失败的主机名


def _unsafe_host(host: str) -> bool:
    """判断给定的 IP 地址字符串是否属于非公网或不安全地址。

    检查范围包括：
    1. IPv4 与 IPv6 的回环地址（Loopback）；
    2. 链路本地地址（Link-local，含 scoped IPv6）；
    3. 私网与局域网地址（如 192.168.x.x、10.x.x.x、IPv6 ULA fc00::/7）；
    4. 组播、保留地址与未指定地址；
    5. 映射为 IPv6 的 IPv4 地址（IPv4-mapped IPv6）。
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = address.ipv4_mapped if address.version == 6 else None
    if mapped is not None and _unsafe_host(str(mapped)):
        return True
    return bool(
        not address.is_global
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_media_url(raw: str, *, require_https: bool = True, resolver=socket.getaddrinfo) -> MediaURLDecision:
    """校验媒体 URL 是否可以安全交付给外部客户端。

    目的：
    防止通过 302 重定向引导客户端或后台探测攻击局域网私有服务。
    
    校验步骤：
    1. 检查是否存在控制字符、反斜杠或多义字符；
    2. 解析 URL 协议，默认必须为 HTTPS；
    3. 检查是否携带用户名密码凭据（userinfo）；
    4. 检查端口号范围是否合法（1-65535）；
    5. 检查主机名本身是否为私网 IP；
    6. 调用 DNS 解析器获取该主机名的全部 IP 地址，任意一个 IP 为私网即立刻拦截。
    """
    value = str(raw or "")
    # 拦截包含 ASCII 控制字符（如换行符、回车符）的非法 URL
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return MediaURLDecision(False, "control_characters")
    # 拦截包含反斜杠或 fragment 标识符的多义 URL
    if "\\" in value or "#" in value:
        return MediaURLDecision(False, "ambiguous_url")
    try:
        parsed = urlsplit(value, allow_fragments=False)
    except ValueError:
        return MediaURLDecision(False, "invalid_url")
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return MediaURLDecision(False, "invalid_scheme")
    if require_https and scheme != "https":
        return MediaURLDecision(False, "scheme_not_https")
    if not parsed.hostname:
        return MediaURLDecision(False, "missing_host")
    if parsed.username is not None or parsed.password is not None:
        return MediaURLDecision(False, "userinfo_present")
    try:
        port = parsed.port
    except ValueError:
        return MediaURLDecision(False, "invalid_port")
    if port is not None and not 1 <= port <= 65535:
        return MediaURLDecision(False, "invalid_port")

    host = parsed.hostname.rstrip(".").lower()
    # 主机名本身若是私网 IP，直接拒绝
    if _unsafe_host(host):
        return MediaURLDecision(False, "host_not_public", host)
    # 进行 DNS 解析，核验所有返回的 IP 地址
    try:
        records = resolver(host, None)
    except (OSError, UnicodeError):
        return MediaURLDecision(False, "dns_unresolved", host)
    addresses = set()
    try:
        for record in records or []:
            addresses.add(str(record[4][0]))
    except (IndexError, TypeError):
        return MediaURLDecision(False, "dns_invalid", host)
    if not addresses:
        return MediaURLDecision(False, "dns_unresolved", host)
    # 只要解析结果中包含任意一个非公网地址，判定为不安全并拦截
    if any(_unsafe_host(address) for address in addresses):
        return MediaURLDecision(False, "resolved_address_not_public", host)
    return MediaURLDecision(True, host=host)


def hostname_for_log(raw: str) -> str:
    """提取 URL 中的主机名供日志打印，绝不包含查询参数与敏感 Token。"""
    try:
        return (urlsplit(str(raw or "")).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""
