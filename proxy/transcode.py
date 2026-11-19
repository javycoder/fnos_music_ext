"""标准音质转码会话：ffmpeg fMP4 HLS 生产与生命周期。

官方 App 选"标准音质"后不再直拉原文件，改走服务端转码 HLS：
POST /track/transcode → GET /track/hls/{guid}/preset.m3u8 → 逐拉 init.mp4 + 000NN.m4s
（fMP4/AAC），期间 10s 心跳、停止 quit。本模块把在线曲目转成同款规格：
ffmpeg 输出 fMP4 分片到 cache/hls/{safe_guid}/，运行中 playlist 由服务端
预合成完整 VOD 清单（实测 App 只拉一次 playlist，不能等转码完再给）。

设计要点：
- 分片用 -hls_flags temp_file：ffmpeg 先写 .tmp 再改名，文件出现即完整，
  服务端无需猜测分片是否写完。
- 合成分片数取 ceil(duration/hls_time)：最后一项指向只读虚拟尾片
  tail-NNNNN.m4s，由服务端顺序拼接该编号到真实结尾的全部分片。不能只声明
  floor 后的真实分片，否则客户端会漏掉最后约一整个 hls_time 的音频
  （issue #131）。
  不设 -hls_init_time：ffmpeg 8 实测它会拖累全部分片按 init 时长切割
  （issue #50：~4s 分片 + 10s 声明清单 → 客户端只播 ~40% 就到清单末尾
  跳歌、seek 到结尾落在媒体时间线外必跳歌）。
- ffmpeg 完成后读取它写出的真实 playlist，按全部分片与真实 EXTINF
  交付最终清单；缓存复用前同样要求清单完整、连续且所有分片存在。
- 会话 heartbeat 续期；超时 / quit 杀进程；完整缓存在配额内 LRU 复用，
  半成品目录（running 中被杀 / failed）直接清理不残留。
- rc==0 不等于产出完整（issue #50）：源站半途断连时 ffmpeg 照样以 0 退出，
  转正前必须校验产出覆盖声明时长；不足的按"元数据虚高/传输截断"分别
  修正常量或弃件重转，磁盘 done 缓存复用同样要求分片数覆盖声明清单。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import time
import uuid
import weakref
from functools import wraps
from dataclasses import dataclass, field

logger = logging.getLogger("fnmusic-ext.transcode")

_SEGMENT_RE = re.compile(r"^\d{5}\.m4s$")
_TAIL_SEGMENT_RE = re.compile(r"^tail-(\d{5})\.m4s$")
INIT_NAME = "init.mp4"
PLAYLIST_NAME = "preset.m3u8"
STATE_NAME = "state.json"
_MAX_PLAYLIST_BYTES = 1024 * 1024

# 测试注入点：假 ffmpeg 脚本路径（ pytest 用，生产读环境探测）
FFMPEG_BIN = os.environ.get("FNMUSIC_FFMPEG_BIN") or shutil.which("ffmpeg")
FFPROBE_BIN = os.environ.get("FNMUSIC_FFPROBE_BIN") or shutil.which("ffprobe")


@dataclass(frozen=True)
class HlsSegment:
    filename: str
    duration_s: float


@dataclass
class Session:
    guid: str
    directory: str
    duration_s: float
    hls_time: float
    declared_count: int
    bitrate: str = "128k"
    proc: asyncio.subprocess.Process | None = None
    status: str = "starting"          # starting|running|done|failed|aborted
    last_beat: float = field(default_factory=time.monotonic)
    started_ts: float = field(default_factory=time.time)
    done_ts: float = 0.0
    exit_event: asyncio.Event = field(default_factory=asyncio.Event)
    watcher: asyncio.Task | None = None
    src: str = ""                     # 转码输入源；产出不足时探测音源真实时长用
    headers: dict | None = None
    rendition: str = field(default_factory=lambda: uuid.uuid4().hex)
    source_fingerprint: dict = field(default_factory=dict)
    on_failure: object = None
    src_kind: str = "online"          # local|online：同一 guid 换版本重建的分片混用防线


# guid → Session；仅存活进程在内，done 会话按磁盘 state.json 复用
_SESSIONS: dict[str, Session] = {}
_SLOT_COND: asyncio.Condition | None = None
_ACTIVE = 0
# Playback outlives ffmpeg: completed disk sessions need a heartbeat/quit lease too.
_PLAYBACK_LEASES: dict[str, tuple[Session, float, float]] = {}
# guid → Lock；弱值字典：持锁协程的局部引用维持条目存活，全部结束后自动回收，
# 常驻进程不随历史 guid 无界累积。
_SESSION_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _serialized(fn):
    @wraps(fn)
    async def locked(guid, *args, **kwargs):
        lock = _SESSION_LOCKS.get(guid)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[guid] = lock
        async with lock:
            return await fn(guid, *args, **kwargs)
    return locked


def _slot_cond() -> asyncio.Condition:
    global _SLOT_COND
    if _SLOT_COND is None:
        _SLOT_COND = asyncio.Condition()
    return _SLOT_COND


def session_dir(root: str, guid: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)
    return os.path.join(root, "hls", safe)


def _read_state(directory: str) -> dict:
    try:
        with open(os.path.join(directory, STATE_NAME), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(sess: Session) -> None:
    try:
        with open(os.path.join(sess.directory, STATE_NAME), "w", encoding="utf-8") as f:
            json.dump({
                "guid": sess.guid,
                "status": sess.status,
                "duration_s": sess.duration_s,
                "hls_time": sess.hls_time,
                "declared_count": sess.declared_count,
                "bitrate": sess.bitrate,
                "started_ts": sess.started_ts,
                "done_ts": sess.done_ts,
                "src_kind": sess.src_kind,
                "rendition": sess.rendition,
                "source_fingerprint": sess.source_fingerprint,
            }, f)
    except Exception as e:  # noqa: BLE001
        logger.warning("write state failed for %s: %s", sess.guid, e)


def _remove_dir(directory: str) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _produced_count(directory: str) -> int:
    """目录里已成形的分片数（temp_file 改名后出现即完整）。"""
    try:
        return sum(1 for name in os.listdir(directory) if _SEGMENT_RE.match(name))
    except OSError:
        return 0


def _usable_cache(directory: str, expected_bitrate: str | None = None) -> dict | None:
    """磁盘上已完整（done + init + 可信真实清单）的转码缓存，返回 state。

    清单必须带 ENDLIST、分片编号从 0 连续且每个文件非空。历史版本把半途
    断流的半成品转正成"完整"缓存（issue #50），或只凭 floor 分片数交付
    漏尾音频（issue #131）；此校验让带毒缓存自愈。
    若指定 expected_bitrate，比特率不匹配的缓存不得混用。
    """
    state = _read_state(directory)
    if state.get("status") != "done":
        return None
    if not os.path.isfile(os.path.join(directory, INIT_NAME)):
        return None
    if expected_bitrate is not None and str(state.get("bitrate") or "").strip() != str(expected_bitrate).strip():
        return None
    segments = read_output_playlist(directory)
    if segments is None:
        return None
    # issue #50 修复前转正的历史毒缓存：源站断连时 ffmpeg rc=0 自然退出并写
    # 出自洽 ENDLIST 清单，但实际分片少于 state 记录的 declared_count——按
    # 新写入语义（finalize 时 declared_count == produced）必然一致，不一致
    # 即历史半成品，拒用并触发重转自愈。
    declared = state.get("declared_count")
    if isinstance(declared, int) and declared > 0 and declared != len(segments):
        return None
    return state


def _declared_count(duration_s: float, hls_time: float) -> int:
    if duration_s <= 0 or hls_time <= 0:
        return 1
    return max(1, math.ceil(duration_s / hls_time - 1e-9))


def read_output_playlist(directory: str) -> tuple[HlsSegment, ...] | None:
    """读取 ffmpeg 已收尾的输出清单；缺失、不完整或不连续一律返回 None。"""
    path = os.path.join(directory, PLAYLIST_NAME)
    try:
        if os.path.getsize(path) > _MAX_PLAYLIST_BYTES:
            return None
        with open(path, encoding="utf-8") as fp:
            lines = [line.strip() for line in fp]
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0] != "#EXTM3U":
        return None

    segments: list[HlsSegment] = []
    pending_duration: float | None = None
    saw_map = False
    saw_end = False
    for line in lines[1:]:
        if not line:
            continue
        if saw_end:
            return None
        if line == "#EXT-X-ENDLIST":
            saw_end = True
            continue
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if not match or match.group(1) != INIT_NAME:
                return None
            saw_map = True
            continue
        if line.startswith("#EXTINF:"):
            if pending_duration is not None:
                return None
            try:
                duration = float(line.partition(":")[2].split(",", 1)[0])
            except ValueError:
                return None
            if not math.isfinite(duration) or duration <= 0:
                return None
            pending_duration = duration
            continue
        if line.startswith("#"):
            continue
        if pending_duration is None or not _SEGMENT_RE.fullmatch(line):
            return None
        index = int(line[:5])
        if index != len(segments):
            return None
        segment_path = os.path.join(directory, line)
        try:
            if os.path.getsize(segment_path) <= 0:
                return None
        except OSError:
            return None
        segments.append(HlsSegment(line, pending_duration))
        pending_duration = None
    if pending_duration is not None or not saw_map or not saw_end or not segments:
        return None
    return tuple(segments)


def _target_duration(segments: tuple[HlsSegment, ...] | list[HlsSegment]) -> int:
    return max(1, math.ceil(max(seg.duration_s for seg in segments) - 1e-9))


def _render_playlist(segments: tuple[HlsSegment, ...] | list[HlsSegment]) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{_target_duration(segments)}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for segment in segments:
        lines.append(f"#EXTINF:{segment.duration_s:.3f},")
        lines.append(segment.filename)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def playlist_text(sess: Session) -> str | None:
    """运行中预合成完整 VOD；已完成会话改用 ffmpeg 真实分片与时长。"""
    if sess.status == "done":
        segments = read_output_playlist(sess.directory)
        if segments is None:
            logger.warning("transcode playlist invalid: guid=%s", sess.guid)
            return None
        return _render_playlist(segments)

    count = max(1, sess.declared_count)
    segments = [
        HlsSegment(f"{index:05d}.m4s", sess.hls_time)
        for index in range(count - 1)
    ]
    tail_index = count - 1
    tail_duration = max(0.001, sess.duration_s - sess.hls_time * tail_index)
    segments.append(HlsSegment(f"tail-{tail_index:05d}.m4s", tail_duration))
    return _render_playlist(segments)


def _ffmpeg_argv(src: str, headers: dict | None, sess: Session, directory: str) -> list[str]:
    argv = [FFMPEG_BIN or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if src.startswith(("http://", "https://")):
        if headers:
            blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            argv += ["-headers", blob]
        ua = (headers or {}).get("User-Agent") or (headers or {}).get("user-agent")
        if ua:
            argv += ["-user_agent", ua]
    argv += [
        "-i", src,
        "-vn", "-map_metadata", "-1",
        "-c:a", "aac", "-b:a", sess.bitrate,
        "-f", "hls",
        "-hls_segment_type", "fmp4",
        "-hls_playlist_type", "event",
        "-hls_flags", "temp_file+independent_segments",
        "-hls_time", str(sess.hls_time),
        "-hls_segment_filename", os.path.join(directory, "%05d.m4s"),
        os.path.join(directory, PLAYLIST_NAME),
    ]
    return argv


async def _watch(sess: Session) -> None:
    global _ACTIVE
    try:
        rc = await sess.proc.wait()
    except Exception:  # noqa: BLE001
        rc = -1
    finally:
        async with _slot_cond():
            _ACTIVE = max(0, _ACTIVE - 1)
            _slot_cond().notify(1)
    if sess.status in ("aborted",):
        _remove_dir(sess.directory)
    elif rc == 0 and os.path.isfile(os.path.join(sess.directory, INIT_NAME)):
        await _finalize_complete_output(sess)
    else:
        logger.warning("transcode failed rc=%s guid=%s", rc, sess.guid)
        sess.status = "failed"
        _remove_dir(sess.directory)
        _SESSIONS.pop(sess.guid, None)
    try:
        if sess.status == "failed" and callable(sess.on_failure):
            sess.on_failure(sess)
    finally:
        sess.exit_event.set()


async def _finalize_complete_output(sess: Session) -> None:
    """rc==0 只是 ffmpeg 的意见，不是产出完整的证明（issue #50）。

    源站谎报 Content-Length 半途断连时，ffmpeg 报 "Stream ends prematurely"
    仍以 0 退出；若按旧逻辑直接转正，playlist 还按元数据时长声明着全量分片，
    客户端播到断点后首个缺失分片 404 跳歌、快进到结尾必 404，且残缺缓存被
    永久复用（每次播放都在同一位置断）。此处按产出覆盖度裁决：
    - 产出时长覆盖声明时长（ffprobe 本地产出，探针误差内）→ 转正；
    - 无 ffprobe 时用真实清单 EXTINF 总时长覆盖声明时长；
    - 音源真实时长 ≈ 产出时长（元数据虚高）→ 按真实时长修正常量后转正，
      重播自然播完，不再向客户端声明永不存在的分片；
    - 其余（传输中途截断）→ 不转正、清目录，下次播放整轨重转。
    """
    segments = read_output_playlist(sess.directory)
    if segments is None:
        logger.warning(
            "transcode output playlist invalid, cache discarded: guid=%s", sess.guid)
        sess.status = "failed"
        _remove_dir(sess.directory)
        _SESSIONS.pop(sess.guid, None)
        return
    produced = len(segments)
    playlist_duration = sum(segment.duration_s for segment in segments)
    real = await probe_duration(os.path.join(sess.directory, PLAYLIST_NAME)) \
        if FFPROBE_BIN else 0.0
    measured = real if real > 0 else playlist_duration
    covered = measured + 2.0 >= sess.duration_s
    if not covered:
        src_dur = await probe_duration(sess.src, sess.headers) \
            if (FFPROBE_BIN and sess.src) else 0.0
        if src_dur > 0 and src_dur <= measured + 3.0:
            logger.warning(
                "transcode metadata duration inflated: guid=%s declared=%.1fs real=%.1fs",
                sess.guid, sess.duration_s, measured)
            sess.duration_s = measured
            sess.declared_count = produced
        else:
            logger.warning(
                "transcode output truncated, cache discarded: guid=%s "
                "produced=%d segs (%.1fs) declared=%d segs (%.1fs) src_probe=%.1fs",
                sess.guid, produced, measured, sess.declared_count,
                sess.duration_s, src_dur)
            sess.status = "failed"
            _remove_dir(sess.directory)
            _SESSIONS.pop(sess.guid, None)
            return
    else:
        sess.duration_s = measured
        sess.declared_count = produced
    sess.status = "done"
    sess.done_ts = time.time()
    # 转正：LRU 访问时间 = 完成时间
    _write_state(sess)
    _SESSIONS.pop(sess.guid, None)
    _touch_state(sess.directory)


def _touch_state(directory: str) -> None:
    try:
        os.utime(os.path.join(directory, STATE_NAME), None)
    except OSError:
        pass


@_serialized
async def ensure_session(
    guid: str,
    src: str,
    duration_s: float,
    *,
    root: str,
    bitrate: str = "128k",
    hls_time: float = 10.0,
    max_active: int = 2,
    max_cache_bytes: int = 512 * 1024 * 1024,
    headers: dict | None = None,
    source_fingerprint: dict | None = None,
    on_failure=None,
) -> Session | None:
    """取或建转码会话；ffmpeg 不可用 / 启动失败返回 None（调用方回落单分片桩）。"""
    directory = session_dir(root, guid)

    alive = _SESSIONS.get(guid)
    if alive and alive.status in ("starting", "running"):
        alive.last_beat = time.monotonic()
        return alive

    if alive and alive.watcher and not alive.watcher.done():
        await asyncio.shield(alive.watcher)
    state = _usable_cache(directory, expected_bitrate=bitrate)
    if state and source_fingerprint is not None and state.get("source_fingerprint") != source_fingerprint:
        state = None
    if state:
        _touch_state(directory)
        return _reused_session_from_state(guid, directory, state, duration_s, hls_time, bitrate)

    if not FFMPEG_BIN:
        return None
    _reap_expired(root=root, ttl_s=1e18)  # 顺带清半成品目录
    await _enforce_quota(root=root, max_bytes=max_cache_bytes)

    if duration_s <= 0:
        duration_s = await probe_duration(src, headers) or 0.0
    if duration_s <= 0:
        duration_s = 240.0

    global _ACTIVE
    async with _slot_cond():
        waited = 0.0
        while _ACTIVE >= max(1, max_active) and waited < 120:
            try:
                await asyncio.wait_for(_slot_cond().wait(), timeout=10.0)
            except asyncio.TimeoutError:
                waited += 10.0
        # 排队超时仍放行：超并发转码好过播放失败
        _ACTIVE += 1

    _remove_dir(directory)
    os.makedirs(directory, exist_ok=True)
    sess = Session(
        guid=guid, directory=directory, duration_s=duration_s,
        hls_time=hls_time, declared_count=_declared_count(duration_s, hls_time),
        bitrate=bitrate, src=src, headers=headers,
        source_fingerprint=source_fingerprint or {}, on_failure=on_failure,
        src_kind="local" if src and not src.startswith(("http://", "https://")) else "online",
    )
    _write_state(sess)
    _SESSIONS[guid] = sess
    try:
        sess.proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_argv(src, headers, sess, directory),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ffmpeg spawn failed for %s: %s", guid, e)
        async with _slot_cond():
            _ACTIVE = max(0, _ACTIVE - 1)
            _slot_cond().notify(1)
        _remove_dir(directory)
        if _SESSIONS.get(guid) is sess:
            _SESSIONS.pop(guid, None)
        return None
    sess.status = "running"
    _SESSIONS[guid] = sess
    sess.watcher = asyncio.create_task(_watch(sess))
    return sess


def get_session(guid: str) -> Session | None:
    sess = _SESSIONS.get(guid)
    if sess and sess.status in ("failed", "aborted"):
        return None
    if sess:
        sess.last_beat = time.monotonic()
    return sess


def _reused_session_from_state(guid: str, directory: str, state: dict,
                               duration_s: float, hls_time: float, bitrate: str) -> Session:
    return Session(
        guid=guid, directory=directory,
        duration_s=float(state.get("duration_s") or duration_s or 0),
        hls_time=float(state.get("hls_time") or hls_time),
        declared_count=int(state.get("declared_count") or 1),
        bitrate=str(state.get("bitrate") or bitrate),
        status="done",
        src_kind=str(state.get("src_kind") or "online"),
        rendition=str(state.get("rendition") or "legacy"),
        source_fingerprint=state.get("source_fingerprint") or {},
    )


def peek_cached(guid: str, root: str) -> Session | None:
    """磁盘上已完整的转码缓存直取（不解析源、不启进程），供重播秒开。"""
    directory = session_dir(root, guid)
    state = _usable_cache(directory)
    if not state:
        return None
    if not state.get("rendition"):
        # 升级遗留缓存无 rendition：按目录确定性铸造，并发首播的两次触碰得到
        # 同一值，不再各持不同 rendition 互踢导致其中一方分片 404。
        state["rendition"] = uuid.uuid5(uuid.NAMESPACE_URL, f"fnmusic-legacy:{directory}").hex
        sess = _reused_session_from_state(guid, directory, state, 0, 10.0, "128k")
        _write_state(sess)
        return sess
    _touch_state(directory)
    return _reused_session_from_state(guid, directory, state, 0, 10.0, "128k")


def _prune_playback_leases() -> None:
    now = time.monotonic()
    for guid, (sess, until, _) in list(_PLAYBACK_LEASES.items()):
        if (now >= until or sess.status in ("failed", "aborted")
                or (sess.status == "done" and not os.path.isfile(os.path.join(sess.directory, INIT_NAME)))):
            _PLAYBACK_LEASES.pop(guid, None)


def lease_playback(sess: Session, ttl_s: float = 90.0) -> None:
    """Pin the exposed rendition until quit or inactivity; no filesystem writes."""
    _prune_playback_leases()
    ttl = max(1.0, float(ttl_s))
    _PLAYBACK_LEASES[sess.guid] = (sess, time.monotonic() + ttl, ttl)


def playback_session(guid: str) -> Session | None:
    _prune_playback_leases()
    lease = _PLAYBACK_LEASES.get(guid)
    return lease[0] if lease else None


def playback_in_use(directory: str | None = None) -> bool:
    _prune_playback_leases()
    return any(directory is None or os.path.abspath(sess.directory) == os.path.abspath(directory)
               for sess, _, _ in _PLAYBACK_LEASES.values())


def heartbeat(guid: str) -> None:
    _prune_playback_leases()
    lease = _PLAYBACK_LEASES.get(guid)
    if lease:
        lease_playback(lease[0], lease[2])
    sess = _SESSIONS.get(guid)
    if sess:
        sess.last_beat = time.monotonic()


@_serialized
async def quit_session(guid: str) -> None:
    """App 主动结束：杀进程；已完整缓存保留复用，半成品清理。"""
    _PLAYBACK_LEASES.pop(guid, None)
    sess = _SESSIONS.pop(guid, None)
    if not sess or not sess.proc:
        return
    if sess.status in ("done", "failed", "aborted"):
        return
    sess.status = "aborted"
    try:
        sess.proc.kill()
    except ProcessLookupError:
        pass
    if sess.watcher:
        await asyncio.shield(sess.watcher)


def session_kind(sess: "Session | None") -> str:
    """会话实际音频来源：local（曲库文件直连）/ online。"""
    if sess is None:
        return ""
    return sess.src_kind or "online"


async def wait_startup(sess: Session, timeout: float = 20.0) -> bool:
    """起播校验：等初始化文件与首个分片成形；会话失败或超时返回 False。

    本地文件输入的会话在返回播放清单前必须通过此校验，失败由调用方清理
    会话后换在线输入重建，避免客户端拿到永不产片的空清单。
    """
    deadline = time.monotonic() + timeout
    while True:
        if sess.status in ("failed", "aborted"):
            return False
        if os.path.isfile(os.path.join(sess.directory, INIT_NAME)):
            if sess.status == "done" or _produced_count(sess.directory) >= 1:
                return True
        if sess.exit_event.is_set():
            return sess.status == "done" and bool(
                os.path.isfile(os.path.join(sess.directory, INIT_NAME)))
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)


@_serialized
async def discard_session(guid: str, expected: Session | None = None) -> None:
    """Wait for the old watcher before allowing reuse of its directory."""
    sess = _SESSIONS.get(guid)
    if sess is None or (expected is not None and sess is not expected):
        return
    _PLAYBACK_LEASES.pop(guid, None)
    sess.status = "aborted"
    if sess.proc and sess.proc.returncode is None:
        try:
            sess.proc.kill()
        except ProcessLookupError:
            pass
    if sess.watcher:
        await asyncio.shield(sess.watcher)
    _remove_dir(sess.directory)
    if _SESSIONS.get(guid) is sess:
        _SESSIONS.pop(guid, None)


def _kill_expired(ttl_s: float) -> None:
    now = time.monotonic()
    for guid, sess in list(_SESSIONS.items()):
        if sess.status in ("starting", "running") and now - sess.last_beat > ttl_s:
            logger.info("transcode session TTL expired: %s", guid)
            sess.status = "aborted"
            try:
                sess.proc.kill()
            except (ProcessLookupError, AttributeError):
                pass


def _reap_expired(root: str, ttl_s: float) -> int:
    """TTL 杀进程 + 清理无主半成品目录；返回清理的目录数。"""
    _kill_expired(ttl_s)
    removed = 0
    hls_root = os.path.join(root, "hls")
    if not os.path.isdir(hls_root):
        return 0
    for name in os.listdir(hls_root):
        d = os.path.join(hls_root, name)
        if not os.path.isdir(d):
            continue
        if _usable_cache(d):
            continue
        # 半成品且无在管进程（内存注册表里没有指向它的会话）
        if not playback_in_use(d) and not any(s.directory == d for s in _SESSIONS.values()):
            _remove_dir(d)
            removed += 1
    return removed


def _dir_size(directory: str) -> int:
    total = 0
    for name in os.listdir(directory):
        try:
            total += os.path.getsize(os.path.join(directory, name))
        except OSError:
            pass
    return total


async def _enforce_quota(root: str, max_bytes: int) -> None:
    """超出配额时按 state.json 的 atime（LRU）逐个删完整缓存目录。"""
    hls_root = os.path.join(root, "hls")
    if not os.path.isdir(hls_root):
        return
    entries = []
    total = 0
    for name in os.listdir(hls_root):
        d = os.path.join(hls_root, name)
        if not os.path.isdir(d) or not _usable_cache(d):
            continue
        size = _dir_size(d)
        try:
            atime = os.path.getmtime(os.path.join(d, STATE_NAME))
        except OSError:
            atime = 0
        entries.append((atime, d, size))
        total += size
    if total <= max_bytes:
        return
    for atime, d, size in sorted(entries):
        if total <= max_bytes:
            break
        if playback_in_use(d):
            continue
        logger.info("transcode cache LRU evict: %s (%d bytes)", d, size)
        _remove_dir(d)
        total -= size


async def probe_duration(src: str, headers: dict | None = None) -> float:
    """ffprobe 取时长（秒）；失败返回 0。"""
    if not FFPROBE_BIN:
        return 0.0
    argv = [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", src]
    if src.startswith(("http://", "https://")) and headers:
        blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        argv[2:2] = ["-headers", blob]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        return float(out.decode().strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def valid_segment_name(filename: str) -> bool:
    return (filename == INIT_NAME
            or bool(_SEGMENT_RE.fullmatch(filename))
            or bool(_TAIL_SEGMENT_RE.fullmatch(filename)))


def tail_start_index(filename: str) -> int | None:
    match = _TAIL_SEGMENT_RE.fullmatch(filename)
    return int(match.group(1)) if match else None


async def wait_file(path: str, sess: Session | None, timeout: float = 30.0) -> str | None:
    """等完整 HLS 文件落盘；会话失败立即返回。

    ffmpeg 的 temp_file 只覆盖媒体分片，不覆盖 init.mp4；首个分片原子
    发布后初始化文件才必然写完，避免读取空文件或不完整的 MP4 头。
    """
    deadline = time.monotonic() + timeout
    init_file = bool(sess and os.path.basename(path) == INIT_NAME)
    while True:
        if os.path.isfile(path):
            if not init_file or os.path.isfile(os.path.join(sess.directory, "00000.m4s")):
                return path
            if sess.status == "done":
                return None  # 完成的缓存缺少首片，不会再有写入
        if sess and sess.exit_event.is_set() and sess.status != "done":
            return None
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)


async def wait_tail_segments(
    sess: Session,
    start_index: int,
    timeout: float = 30.0,
) -> tuple[HlsSegment, ...] | None:
    """等转码收尾后返回 start_index 到真实结尾的连续分片。"""
    if start_index < 0:
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if sess.status in ("failed", "aborted"):
            return None
        if sess.status == "done":
            segments = read_output_playlist(sess.directory)
            if segments is None or start_index >= len(segments):
                return None
            tail = segments[start_index:]
            for offset, segment in enumerate(tail, start=start_index):
                if segment.filename != f"{offset:05d}.m4s":
                    return None
            return tail
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            await asyncio.wait_for(sess.exit_event.wait(), timeout=min(0.5, remaining))
        except asyncio.TimeoutError:
            pass
        if sess.exit_event.is_set() and sess.status != "done":
            return None


async def maintain(root: str, ttl_s: float, max_bytes: int) -> None:
    """心跳/播放请求顺带触发的维护：TTL 回收 + 配额 LRU。"""
    _reap_expired(root=root, ttl_s=ttl_s)
    await _enforce_quota(root=root, max_bytes=max_bytes)


async def shutdown_all() -> None:
    _PLAYBACK_LEASES.clear()
    for guid in list(_SESSIONS):
        await quit_session(guid)
