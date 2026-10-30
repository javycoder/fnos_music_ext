"""直连播放（Direct Play）目标状态与同表示续传绑定模块。

本模块属于 proxy 目录。主要维护短期直连目标的缓存与绑定关系：
1. DirectMediaTarget：记录解析到的音频媒体真实 URL、格式与复用有效时间；
2. DirectPlayBinding：记录特定客户端凭据作用域与媒体目标的绑定快照；
3. DirectTargetBindings：有界内存存储，保证客户端后续 Range 续传访问同一音频版本；
4. FileTargetBindings：记录本地曲库文件指纹，防止播放过程中文件被标签改写导致偏移错位。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


@dataclass
class DirectMediaTarget:
    """短期直连播放目标的数据结构。

    封装上游音乐源解析得到的媒体真实地址及安全元数据。
    """
    provider: str  # 音源提供者名称（例如 netease、lx 或 musicdl）
    platform: str  # 平台标识（例如 kg、qq、netease）
    canonical_id: str  # 歌曲标准 ID
    url: str  # 外部媒体直接播放 HTTPS 地址
    protocol: str = "https"  # 媒体传输协议
    server_headers: dict[str, str] = field(default_factory=dict)  # 源站要求的特定请求头
    ext: str = ""  # 音频扩展名（例如 flac、mp3、wav）
    actual_quality: str = ""  # 实际解析音质档位
    file_size: int | None = None  # 音频文件总字节数
    resolved_at: float = field(default_factory=time.time)  # 解析完成时间戳
    reuse_deadline: float = 0.0  # 该直连目标在本地代理的最长可复用单调时间
    upstream_expires_at: float | None = None  # 上游源站声明的到期时间戳
    expiry_source: str = "unknown"  # 有效期来源（例如 policy 或 upstream）
    source_generation: int = 0  # 音源配置代号，配置变化时代号递增
    representation_key: str = ""  # 媒体表示指纹，区分不同转码或不同来源
    selection_id: str = ""  # 内部下载会话 ID，供后台保存时复用同一流


@dataclass(frozen=True)
class DirectPlayBinding:
    """客户端请求作用域与媒体目标的绑定项。"""
    target: DirectMediaTarget  # 绑定的媒体目标
    expires_at: float  # 本次绑定失效的单调时间戳
    generation: int  # 建立绑定时的音源代号


class DirectTargetBindings:
    """有界的同版本目标绑定存储器。

    目的：
    客户端发送非零 Range 续传请求时，必须连接到与前序请求相同的音频版本。
    本类按客户端凭据作用域与歌曲 GUID 隔离并锁定目标媒体，防止音频版本错乱。
    """

    def __init__(self, max_entries: int = 512):
        self.max_entries = max_entries  # 最大缓存条目数量
        self._entries: dict[str, DirectPlayBinding] = {}  # 活跃绑定字典
        self._known: dict[str, None] = {}  # 历史登记顺序队列，用于 LRU 淘汰

    def get(self, scope: str, guid: str) -> DirectPlayBinding | None:
        """获取当前作用域与歌曲 GUID 绑定的有效直连目标。"""
        key = f"{scope}\0{guid}"
        binding = self._entries.get(key)
        if binding is None:
            return None
        # 若绑定已超时，或音源全局代号发生变更，立即使其失效并移除
        if time.monotonic() >= binding.expires_at or binding.generation != binding.target.source_generation:
            self._entries.pop(key, None)
            return None
        return binding

    def known(self, scope: str, guid: str) -> bool:
        """检查特定作用域与歌曲是否曾经建立过直连绑定。"""
        return f"{scope}\0{guid}" in self._known

    def forget(self, scope: str, guid: str) -> None:
        """清理特定作用域与歌曲的绑定记录。"""
        key = f"{scope}\0{guid}"
        self._entries.pop(key, None)
        self._known.pop(key, None)

    def put(self, scope: str, guid: str, target: DirectMediaTarget, generation: int, ttl: float) -> DirectPlayBinding:
        """保存新的直连目标绑定，并维护 LRU 容量限制。"""
        now = time.monotonic()
        # 音源代号变更时清除旧代号的全部绑定
        self.invalidate_source(target.provider, generation)
        # 移除已过期的条目
        for expired in [key for key, value in self._entries.items() if value.expires_at <= now]:
            self._entries.pop(expired, None)
        # 绑定的有效期不得超过目标本身允许的最长复用期限
        binding = DirectPlayBinding(target, min(now + max(0.0, ttl), target.reuse_deadline), generation)
        key = f"{scope}\0{guid}"
        self._entries[key] = binding
        self._known.pop(key, None)
        self._known[key] = None
        # 超出容量限制时淘汰最早记录
        while len(self._known) > self.max_entries:
            oldest = next(iter(self._known))
            self._known.pop(oldest, None)
            self._entries.pop(oldest, None)
        return binding

    def invalidate_source(self, provider: str, generation: int) -> None:
        """当某个音源重新配置导致代号递增时，清理该音源旧代号的所有绑定。"""
        for key in list(self._entries):
            binding = self._entries[key]
            if binding.target.provider == provider and binding.generation != generation:
                self._entries.pop(key, None)


class FileTargetBindings:
    """本地曲库文件指纹绑定器。

    目的：
    当客户端播放本地文件或已下载曲目时，记录文件的设备号、Inode 与修改时间。
    若后台任务在此期间对文件重写了 ID3 标签，文件大小与偏移发生改变，
    后续非零 Range 请求通过指纹比对能及时感知并拒绝续传，防止播放杂音。
    """

    def __init__(self, max_entries: int = 512):
        self.max_entries = max_entries
        self._entries: dict[tuple[str, str], tuple] = {}

    @staticmethod
    def fingerprint(path: str) -> tuple:
        """获取目标文件的状态指纹（路径、设备号、Inode、大小、修改纳秒）。"""
        stat = os.stat(path)
        return path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def remember(self, scope: str, guid: str, path: str) -> None:
        """记录文件指纹，有效期 300 秒。"""
        key = (scope, guid)
        self._entries.pop(key, None)
        self._entries[key] = (time.monotonic() + 300.0, self.fingerprint(path))
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)), None)

    def forget(self, scope: str, guid: str) -> None:
        """清除指定文件的指纹记录。"""
        self._entries.pop((scope, guid), None)

    def matches(self, scope: str, guid: str, path: str | None = None) -> bool:
        """比对当前文件的实时指纹是否与初次播放时记录的指纹一致。"""
        entry = self._entries.get((scope, guid))
        if entry is None or entry[0] <= time.monotonic():
            return False
        try:
            return entry[1] == self.fingerprint(path or entry[1][0])
        except OSError:
            return False
