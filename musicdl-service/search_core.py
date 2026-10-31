"""单源搜索任务的纯执行逻辑：库调用、曲目归一化与直链探活。

只在 worker 子进程（worker.py）内运行；父进程 app.py 只做缓存与 API 编排，
不再直接调用 musicdl 库——卡死的上游调用随子进程一起被回收，不会永久
占用音源坑位。函数保持进程内可测：库调用与探活都是本模块顶层函数，
测试可直接 monkeypatch。配置读同一组 MUSICDL_* 环境变量，与 app.py 对齐。

网络预算完全依赖父进程的进程级回收（TERM/KILL worker），不使用
socket.setdefaulttimeout 这类进程全局副作用。
"""
import logging
import os
import time
from http.cookies import SimpleCookie
from http.cookiejar import CookieJar
from urllib.request import Request
from urllib.parse import urlsplit, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from contextlib import ExitStack

import httpx

logger = logging.getLogger("musicdl_worker")

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False
    logger.warning("curl_cffi not installed, falling back to standard httpx client")

CFG = {
    "work_dir": os.environ.get("MUSICDL_WORK_DIR", "/tmp/musicdl_outputs"),
    # 库层网络非活动超时，与整任务预算（父进程下发）分工
    "request_timeout": max(0.1, float(os.environ.get("MUSICDL_REQUEST_TIMEOUT", "5"))),
    # 库按这个数量翻页；一页 10 首大约 4 秒，封顶保证整次 search() 能在预算内返回
    "search_page_cap": max(1, int(os.environ.get("MUSICDL_SEARCH_PAGE_CAP", "10"))),
    "probe_timeout": max(0.2, float(os.environ.get("MUSICDL_PROBE_TIMEOUT", "2"))),
    # 回退 GET 只读这么多个字节判断 magic，不读整首曲目
    "probe_max_bytes": max(256, int(os.environ.get("MUSICDL_PROBE_MAX_BYTES", "1024"))),
}

TRIAL_MARKERS = (
    "(试听)",
    "（试听）",
    "试听片段",
    "片段试听",
    "试听版",
    "[试听]",
    "【试听】",
    "- 试听",
    " - 试听",
)

# 已知自身实现里「显式 timeout 之后又展开 **request_overrides」的平台（musicdl 2.14.2）。
# 例如 modules/sources/soda.py 第 87/105/109/111 行形如：
#     requests.post(url, ..., timeout=10, **request_overrides)
# 项目再注入同名 timeout 会触发 Python 重复关键字 TypeError，因此这些源不再注入。
# 其余源没有这个冲突，保留明确的超时，避免上游调用无限悬挂。
SOURCES_WITH_CONFLICTING_TIMEOUT = frozenset({"SodaMusicClient"})

# SongInfo.protocol 只有 HTTP / HLS 两种取值；其它协议需要专门客户端，不是裸媒体。
DIRECT_PLAY_PROTOCOLS = frozenset({"HTTP"})

# 加密/DRM 容器：URL 拿到的是密文，必须走 SDK 后处理。这里明确记为不支持，
# 不实现任何绕过或解密，也不对外提供加密流的间接下载。
ENCRYPTED_EXTS = frozenset({
    "ncm", "qmc0", "qmc3", "qmcflac", "qmcogg", "kgm", "kwm", "vpr", "tkm",
    "mflac", "mgg", "mg3d", "bkcmp3", "bkcflac", "aax", "aaxc", "m4p",
})

# 播放/下载需要服务端后处理（解密、HLS 拉流合并）的原始标记。
# raw_data.play_auth 非空表示正文被 DRM 保护，必须经 SDK._download 后处理。
AUTH_KEYS = ("play_auth", "PlayAuth")
DRM_KEYS = ("drm", "drm_token", "encrypted", "encrypted_contents", "decrypt_key")

# 七档音质表（与 REQUIREMENTS 一致）。实际 br 未知时不猜档位，只报 unknown。
UNKNOWN_QUALITY = "unknown"

def source_short(client_name: str) -> str:
    return (client_name or "").replace("MusicClient", "").lower()


def describe_task_error(exc: BaseException, stage: "str | None") -> str:
    """诊断只保留类型与阶段；自由正文可能含未知签名、Cookie或Authorization。

    凭据格式无法枚举，不能在跨进程错误协议里回传替换过几个键的原异常。
    包装异常仍保留原始类型，便于区分TypeError、超时和依赖缺失。
    """
    stage_name = stage or getattr(exc, "stage", "")
    if not isinstance(stage_name, str) or stage_name not in {"search", "probe", "normalize", "resolve", "stream"}:
        stage_name = "unspecified"
    error_type = type(exc).__name__
    cause = getattr(exc, "cause", None)
    if isinstance(cause, BaseException):
        error_type += f"/{type(cause).__name__}"
    return f"{error_type} (stage={stage_name})"


class SearchStageError(Exception):
    """搜索阶段（库调用）失败：包住原始异常并保留 phase。"""

    stage = "search"

    def __init__(self, cause: BaseException):
        super().__init__(describe_task_error(cause, self.stage))
        self.cause = cause


class ProbeStageError(Exception):
    """探活阶段失败：包住原始异常并保留 phase。"""

    stage = "probe"

    def __init__(self, cause: BaseException):
        super().__init__(describe_task_error(cause, self.stage))
        self.cause = cause


def media_unsupported_reason(item: dict) -> "str | None":
    """统一判据：这首歌是否可以当作普通 HTTP 裸媒体交付。

    搜索初筛、/stream 与 /internal/resolve 共用这个函数，避免只在某一条
    路径上拦截、另一条路径仍把需要后处理的条目当作 mp3 直链发出去。
    """
    if not isinstance(item, dict):
        return "invalid item"
    if item.get("delivery") == "unsupported":
        return str(item.get("unsupported_reason") or "requires server-side post-processing")
    if str(item.get("protocol") or "http").upper() not in DIRECT_PLAY_PROTOCOLS:
        return f"protocol {item.get('protocol')!r} is not a plain HTTP stream"
    ext = str(item.get("ext") or "").lower().lstrip(".")
    if ext in ENCRYPTED_EXTS:
        return f"encrypted container .{ext}"
    url = str(item.get("download_url") or "")
    if url and not url.startswith(("http://", "https://")):
        return "download_url is not http(s)"
    try:
        path = unquote(urlsplit(url).path).lower()
    except ValueError:
        return "invalid media URL"
    # HLS/DASH通常也使用HTTP；仅检查URL scheme或SDK的HTTP标签会把清单伪装为MP3。
    if ext in {"m3u8", "mpd", "hls"} or path.endswith((".m3u8", ".mpd")):
        return "streaming manifest requires dedicated processing"
    return None


def _raw_flag(raw: dict, keys) -> bool:
    """raw_data 里是否有非空标记（键名大小写与常见别名都容纳）。"""
    if not isinstance(raw, dict):
        return False
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (dict, list)) and value:
            return True
        if value is True:
            return True
    return False


def _quality_fields(song) -> tuple:
    """从真实 SongInfo 读实际码率/采样率，不靠扩展名或平台能力猜测档位。"""
    bitrate = getattr(song, "bitrate", None)
    try:
        bitrate = int(bitrate) if bitrate is not None else 0
    except (TypeError, ValueError):
        bitrate = 0
    samplerate = getattr(song, "samplerate", None)
    try:
        samplerate = int(samplerate) if samplerate is not None else 0
    except (TypeError, ValueError):
        samplerate = 0
    codec = str(getattr(song, "codec", "") or "").strip().lower()
    channels = getattr(song, "channels", None)
    return bitrate, samplerate, codec, channels


def normalize(song, keyword: str) -> dict:
    """把 musicdl SongInfo 投影成对外契约。

    保留实际 protocol、媒体格式与「是否需要后处理」状态；服务端 headers/cookies
    与 raw_data 留在本服务缓存里，绝不写进对外条目（泄漏源站凭据）。
    """
    src = source_short(getattr(song, "source", "") or "")
    sid = str(getattr(song, "identifier", "") or "")
    raw = getattr(song, "raw_data", None) or {}
    protocol = str(getattr(song, "protocol", "") or "HTTP").strip().upper() or "HTTP"
    ext = str(getattr(song, "ext", "") or "").strip().lower().lstrip(".") or "mp3"
    if not isinstance(raw, dict):
        raw = {}
    bitrate, samplerate, codec, channels = _quality_fields(song)

    # 交付形态判定：优先按「必须后处理/受保护」判定，再落到普通 HTTP。
    delivery = "direct"
    reason = None
    if _raw_flag(raw, AUTH_KEYS):
        delivery = "unsupported"
        reason = "play_auth present: stream requires SDK post-processing"
    elif _raw_flag(raw, DRM_KEYS):
        delivery = "unsupported"
        reason = "DRM/encrypted payload requires SDK post-processing"
    elif protocol not in DIRECT_PLAY_PROTOCOLS:
        delivery = "unsupported"
        reason = f"protocol {protocol} requires a dedicated client (e.g. HLS)"
    elif ext in ENCRYPTED_EXTS:
        delivery = "unsupported"
        reason = f"encrypted container .{ext} requires SDK post-processing"

    item = {
        "id": f"{src}:{sid}",
        "source": src,
        "title": getattr(song, "song_name", "") or "",
        "artist": getattr(song, "singers", "") or "",
        "album": getattr(song, "album", "") or "",
        "duration_s": getattr(song, "duration_s", 0) or 0,
        "ext": ext,
        "file_size": getattr(song, "file_size_bytes", 0) or 0,
        # 实际协议：HLS/m3u8 不能被当成 mp3 直链
        "protocol": protocol.lower(),
        # 交付形态：direct=可当作裸 HTTP 媒体；unsupported=需后处理/受保护，禁止裸链转发
        "delivery": delivery,
        "unsupported_reason": reason,
        # 实际品质：bitrate 未知就是 unknown，不按扩展名或平台能力猜 Hi-Res/母带
        "bitrate": bitrate,
        "samplerate": samplerate,
        "codec": codec,
        "channels": channels if isinstance(channels, int) else 0,
        "quality": UNKNOWN_QUALITY,
        "cover_url": getattr(song, "cover_url", "") or "",
        # qq 曲目的专辑封面主键：封面兜底链用 albummid 直构 gtimg 图床 URL
        "albummid": str(getattr(song, "albummid", "") or getattr(song, "album_mid", "") or ""),
        "download_url": getattr(song, "download_url", "") or "",
    }
    reason = media_unsupported_reason(item)
    if reason:
        item["delivery"] = "unsupported"
        item["unsupported_reason"] = reason
    return item


def is_candidate_playable(song, item: dict) -> bool:
    """初筛：id/标题有效后，还要按统一判据排除需后处理、受保护或非 HTTP 协议。

    与 /stream、/internal/resolve 共用 media_unsupported_reason，避免
    「只在搜索结果里过滤、直链接口仍会发出受保护链接」的不一致。
    """
    sid = item.get("id") or ""
    if not sid or sid.endswith(":"):
        return False
    title = str(item.get("title") or "")
    if not title:
        return False
    if any(marker in title for marker in TRIAL_MARKERS):
        return False
    url = str(item.get("download_url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return False
    if "404/error.html" in url or "error.html" in url:
        return False
    return media_unsupported_reason(item) is None


def _download_request_headers(song) -> dict:
    """保留SDK提供的私有Cookie；显式Cookie头优先，不修改共享SongInfo请求头。"""
    headers = dict(getattr(song, "default_download_headers", None) or {})
    cookies = getattr(song, "default_download_cookies", None)
    if not cookies or any(str(key).lower() == "cookie" for key in headers):
        return headers
    # RequestsCookieJar及curl_cffi.Cookies.jar保留各自作用域，不能合并后发给任意CDN。
    scoped_jar = cookies if isinstance(cookies, CookieJar) else getattr(cookies, "jar", None)
    if isinstance(scoped_jar, CookieJar):
        request = Request(str(getattr(song, "download_url", "") or ""))
        scoped_jar.add_cookie_header(request)
        value = request.get_header("Cookie")
        if value:
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("invalid download cookie characters")
            headers["Cookie"] = value
        return headers
    items = getattr(cookies, "items", None)
    if not callable(items):
        raise TypeError("unsupported download cookie container")
    jar = SimpleCookie()
    for name, value in items():
        if value is None:
            continue
        name, value = str(name), str(value)
        if any(ord(char) < 32 or ord(char) == 127 for char in name + value):
            raise ValueError("invalid download cookie characters")
        jar[name] = value
    if jar:
        headers["Cookie"] = "; ".join(morsel.OutputString() for morsel in jar.values())
    return headers


def request_overrides_for(source: str) -> dict:
    """构造传给 musicdl 的 requests_overrides：冲突源不再注入 timeout。

    musicdl 2.14.2 的部分源在自己的 requests 调用里写了显式 `timeout=10`
    再展开 `**request_overrides`；外部再传同名 timeout 会触发 Python
    重复关键字 TypeError，使整次搜索在一次调用里炸掉。
    """
    if source in SOURCES_WITH_CONFLICTING_TIMEOUT:
        return {}
    return {"timeout": CFG["request_timeout"]}


def _build_client(source: str, cfg: dict, overrides: dict):
    """单独的构造点：测试可拦截这里检查真实传给 SDK 的参数。"""
    from musicdl import musicdl

    return musicdl.MusicClient(
        music_sources=[source],
        init_music_clients_cfg={source: cfg},
        clients_threadings={source: 1},
        requests_overrides={source: overrides},
    )


def search_one_source(source: str, keyword: str, limit: int) -> list:
    """单个源搜索（worker 进程内执行，阻塞）。

    网络超时只作为「库层非活动超时」存在；整体预算由父进程回收 worker
    进程保证，不使用 socket.setdefaulttimeout 之类的全局副作用。
    """
    cfg = {
        # 30 条限额会变成 6 页，整次 search() 返回前一首都交不出来。
        # 封顶一页，够首屏，也放得进超时。
        "search_size_per_source": min(max(int(limit or 1), 1), int(CFG["search_page_cap"])),
        "work_dir": CFG["work_dir"],
        "max_retries": 1,
    }
    client = _build_client(source, cfg, request_overrides_for(source))
    result = client.search(keyword=keyword)
    return list(result.values())[0] if result else []


def _looks_like_media(sample: bytes, content_type: str) -> bool:
    """用实际字节前缀判断是不是媒体，而不是只看 Content-Type。

    JSON/HTML 错误体必须显式拒绝：403/405 回退经常返回这种响应，
    只看状态码会把错误页当成可播放成就。
    """
    ct = (content_type or "").strip().lower()
    head = (sample or b"")[:512]
    stripped = head.lstrip()
    if stripped.startswith(b"\xef\xbb\xbf"):
        stripped = stripped[3:].lstrip()
    # XML错误与HLS清单即使被错标audio/mpeg也不是可直接交付的音频。
    if stripped[:1] in (b"{", b"[", b"<") or stripped.startswith(b"#EXTM3U"):
        return False
    if ("application/json" in ct or "text/json" in ct or ct.startswith("text/")
            or "xml" in ct or "mpegurl" in ct):
        return False
    if not sample:
        return False
    # 只确认常见音频容器签名；未知二进制不能仅凭audio/*或octet-stream当作可播。
    signatures = (
        b"ID3", b"fLaC", b"OggS", b"MAC ", b"wvpk", b"MPCK", b"MP+",
        b"TTA1", b"caff", b"tBaK", b"DSD ", b"FRM8", b".RMF", b"#!AMR", b"ADIF",
        bytes.fromhex("1a45dfa3"), bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c"),
        bytes.fromhex("7ffe8001"), bytes.fromhex("fe7f0180"), bytes.fromhex("0b77"),
    )
    if stripped.startswith(signatures):
        return True
    if len(stripped) >= 8 and stripped[4:8] == b"ftyp":
        return True
    if len(stripped) >= 12:
        if stripped[:4] in (b"RIFF", b"RIFX", b"RF64") and stripped[8:12] == b"WAVE":
            return True
        if stripped[:4] == b"FORM" and stripped[8:12] in (b"AIFF", b"AIFC"):
            return True
    # MPEG audio/ADTS AAC与LOAS只判断同步头；完整解码仍由交付阶段或独立验收完成。
    return (len(stripped) >= 2 and (
        (stripped[0] == 0xFF and stripped[1] & 0xE0 == 0xE0)
        or (stripped[0] == 0x56 and stripped[1] & 0xE0 == 0xE0)))


def _accept_response(status: int, content_type: str, sample: bytes) -> bool:
    if status not in (200, 206):
        return False
    return _looks_like_media(sample, content_type)


_PROBE_RANGE_TEMPLATE = "bytes=0-{end}"


def _probe_range_header(max_bytes: int) -> str:
    return _PROBE_RANGE_TEMPLATE.format(end=max(0, int(max_bytes) - 1))


def _read_prefix(iterator, max_bytes: int) -> bytes:
    """只读前面 max_bytes 个字节后立即停止，不读完整首曲目。"""
    buf = b""
    for chunk in iterator:
        if not chunk:
            continue
        buf += chunk
        if len(buf) >= max_bytes:
            break
    return buf[:max_bytes]


def probe_playable_sync(url: str, headers: dict) -> bool:
    """检查直链状态与可识别音频前缀，不冒充整曲解码或客户端播放验证。

    HEAD成功仍须读取实际前缀；HEAD403/405也允许GET，因为部分CDN拒绝HEAD。
    GET请求Range 0-1023，HTTPX读取前缀后关闭响应；curl使用写回调直接中止，
    避免stream.close等待仍在接收整曲的线程。

    签名保持 (url, headers)：既有调用方与测试替身都按两参数装配。
    """
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    if "404/error.html" in url or "error.html" in url:
        return False
    hdrs = {k: v for k, v in (headers or {}).items() if k.lower() != "range"}
    max_bytes = max(256, int(CFG.get("probe_max_bytes", 1024)))
    if HAS_CURL_CFFI:
        try:
            with ExitStack() as responses:
                r = curl_requests.head(
                    url,
                    headers=hdrs,
                    impersonate="chrome",
                    allow_redirects=True,
                    timeout=CFG["probe_timeout"],
                )
                responses.callback(r.close)
                if r.status_code in (200, 206, 403, 405):
                    hdrs["Range"] = _probe_range_header(max_bytes)
                    prefix = bytearray()
                    stopped_at_limit = False
                    # libcurl标准中止值；curl_cffi会忽略普通短写(包括0)，不能用0代替。
                    write_abort = 0xFFFFFFFF
                    def consume(chunk):
                        nonlocal stopped_at_limit
                        remaining = max_bytes - len(prefix)
                        if remaining > 0:
                            prefix.extend(chunk[:remaining])
                        if len(prefix) >= max_bytes:
                            stopped_at_limit = True
                            return write_abort  # 不创建等待整曲的流线程，触发可识别的WRITE_ERROR。
                        return len(chunk)
                    try:
                        r = curl_requests.get(
                            url, headers=hdrs, impersonate="chrome", allow_redirects=True,
                            content_callback=consume, timeout=CFG["probe_timeout"],
                        )
                    except Exception as error:
                        r = getattr(error, "response", None)
                        # 仅把本次主动截断产生的WRITE_ERROR当正常结束；真实网络错误仍失败。
                        if not stopped_at_limit or getattr(error, "code", None) != 23 or r is None:
                            raise
                    responses.callback(r.close)
                    return _accept_response(r.status_code, r.headers.get("content-type"), bytes(prefix))
                return False
        except Exception:
            return False
    else:
        try:
            with httpx.Client(follow_redirects=True, timeout=CFG["probe_timeout"]) as cx:
                r = cx.head(url, headers=hdrs)
                if r.status_code in (200, 206, 403, 405):
                    hdrs["Range"] = _probe_range_header(max_bytes)
                    with cx.stream("GET", url, headers=hdrs) as response:
                        sample = _read_prefix(response.iter_bytes(max_bytes), max_bytes)
                        return _accept_response(response.status_code,
                                                response.headers.get("content-type"), sample)
                return False
        except Exception:
            return False


def run_search_task(task: dict, emit, on_stage=None) -> dict:
    """执行一个搜索任务：搜索 → 归一化初筛 → 并行探活，确认一首上交一首。

    emit(entry) 的 entry 是 [item, download_headers, lyric]（JSON 行协议里的
    数组形态）。返回 {"count": n, "partial": bool}；partial=True 表示探活
    没在预算内跑完（父进程收到后不复用本 worker）。

    on_stage 可选回调：进入每个阶段时收到阶段名（"search"/"probe"）。失败时
    异常包裹为带阶段的 SearchStageError / ProbeStageError，让 worker 能向上
    报出可定位的阶段而不是塌成同一个「task failed」。
    """
    source = str(task.get("source") or "")
    keyword = str(task.get("keyword") or "")
    fetch_size = max(1, int(task.get("fetch_size") or 10))
    limit = max(1, int(task.get("limit") or 1))
    song_id = task.get("song_id") or None
    budget_s = float(task.get("budget_s") or 0)
    deadline = time.monotonic() + budget_s
    stage = "search"

    def enter(phase: str):
        nonlocal stage
        stage = phase
        if on_stage is not None:
            on_stage(phase)

    if deadline is not None and time.monotonic() >= deadline:
        return {"count": 0, "partial": True}
    try:
        songs = search_one_source(source, keyword, fetch_size)
    except SearchStageError:
        raise
    except BaseException as exc:  # noqa: BLE001 统一转成带阶段的搜索异常
        raise SearchStageError(exc) from exc
    candidates = []
    for song in songs[:fetch_size]:
        if not song:
            continue
        item = normalize(song, keyword)
        if song_id is not None and item["id"] != song_id:
            continue
        if not is_candidate_playable(song, item):
            continue
        headers = _download_request_headers(song)
        lyric = str(getattr(song, "lyric", "") or "")
        candidates.append((item, headers, lyric))
    if not candidates:
        return {"count": 0, "partial": False}
    if deadline is not None and time.monotonic() >= deadline:
        # 预算已在搜索阶段耗尽：不再起探活，交白卷并标记 partial
        return {"count": 0, "partial": True}

    enter("probe")
    valid = set()
    partial = False
    futures = {}
    pool = ThreadPoolExecutor(max_workers=min(8, len(candidates)), thread_name_prefix="probe")
    try:
        for idx, (item, headers, _) in enumerate(candidates):
            futures[pool.submit(probe_playable_sync, item["download_url"], headers)] = idx
        try:
            wait_budget = None if deadline is None else max(0.05, deadline - time.monotonic())
            for fut in as_completed(futures, timeout=wait_budget):
                idx = futures[fut]
                if fut.result():
                    valid.add(idx)
                    item, headers, lyric = candidates[idx]
                    emit([item, headers, lyric])
                    if len(valid) >= limit:
                        break
        except FuturesTimeoutError:
            partial = True
        except BaseException as exc:  # noqa: BLE001 探活阶段的失败要带上 stage
            raise ProbeStageError(exc) from exc
        finally:
            for fut in futures:
                fut.cancel()

    finally:
        # Never join blocked probes here. Parent recycles this process before release.
        pool.shutdown(wait=False, cancel_futures=True)
        # Even completed futures do not prove executor threads have exited.
        join_deadline = time.monotonic() + 0.005
        for thread in pool._threads:
            thread.join(max(0, join_deadline - time.monotonic()))
        partial = partial or any(thread.is_alive() for thread in pool._threads)
    partial = partial or any(not future.done() for future in futures)
    return {"count": len(valid), "partial": partial}
