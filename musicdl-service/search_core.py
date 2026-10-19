"""单源搜索任务的纯执行逻辑：库调用、曲目归一化与直链探活。

只在 worker 子进程（worker.py）内运行；父进程 app.py 只做缓存与 API 编排，
不再直接调用 musicdl 库——卡死的上游调用随子进程一起被回收，不会永久
占用音源坑位。函数保持进程内可测：库调用与探活都是本模块顶层函数，
测试可直接 monkeypatch。配置读同一组 MUSICDL_* 环境变量，与 app.py 对齐。
"""
import logging
import os
import time
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


def source_short(client_name: str) -> str:
    return (client_name or "").replace("MusicClient", "").lower()


def normalize(song, keyword: str) -> dict:
    src = source_short(getattr(song, "source", "") or "")
    sid = str(getattr(song, "identifier", "") or "")
    return {
        "id": f"{src}:{sid}",
        "source": src,
        "title": getattr(song, "song_name", "") or "",
        "artist": getattr(song, "singers", "") or "",
        "album": getattr(song, "album", "") or "",
        "duration_s": getattr(song, "duration_s", 0) or 0,
        "ext": getattr(song, "ext", "") or "mp3",
        "file_size": getattr(song, "file_size_bytes", 0) or 0,
        "cover_url": getattr(song, "cover_url", "") or "",
        # qq 曲目的专辑封面主键：封面兜底链用 albummid 直构 gtimg 图床 URL
        "albummid": str(getattr(song, "albummid", "") or getattr(song, "album_mid", "") or ""),
        "download_url": getattr(song, "download_url", "") or "",
    }


def is_candidate_playable(song, item: dict) -> bool:
    """初筛：检查 identifier、标题试听标记、download_url 存在且非错误页。"""
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
    return True


def search_one_source(source: str, keyword: str, limit: int) -> list:
    """单个源搜索（worker 进程内执行，阻塞）。"""
    from musicdl import musicdl

    client = musicdl.MusicClient(
        music_sources=[source],
        init_music_clients_cfg={
            source: {
                # 30 条限额会变成 6 页，整次 search() 返回前一首都交不出来。
                # 封顶一页，够首屏，也放得进超时。
                "search_size_per_source": min(max(int(limit or 1), 1), int(CFG["search_page_cap"])),
                "work_dir": CFG["work_dir"],
                "max_retries": 1,
            },
        },
        clients_threadings={source: 1},
        requests_overrides={source: {"timeout": CFG["request_timeout"]}},
    )
    result = client.search(keyword=keyword)
    return list(result.values())[0] if result else []


def probe_playable_sync(url: str, headers: dict) -> bool:
    """探测直链是否有效且真实可播放（过滤无效/试听/404直链/HTML错误页）。"""
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    if "404/error.html" in url or "error.html" in url:
        return False
    hdrs = dict(headers or {})
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
                if r.status_code == 405:
                    hdrs["Range"] = "bytes=0-1"
                    r = curl_requests.get(
                        url,
                        headers=hdrs,
                        impersonate="chrome",
                        allow_redirects=True,
                        stream=True,
                        timeout=CFG["probe_timeout"],
                    )
                    # Stop the unconsumed stream before this worker can be reused.
                    responses.callback(r.close)
                if r.status_code in (200, 206):
                    ct = (r.headers.get("content-type") or "").lower()
                    if "text/html" in ct:
                        return False
                    return True
                return False
        except Exception:
            return False
    else:
        try:
            with httpx.Client(follow_redirects=True, timeout=CFG["probe_timeout"]) as cx:
                r = cx.head(url, headers=hdrs)
                if r.status_code == 405:
                    hdrs["Range"] = "bytes=0-1"
                    r = cx.get(url, headers=hdrs)
                if r.status_code in (200, 206):
                    ct = (r.headers.get("content-type") or "").lower()
                    if "text/html" in ct:
                        return False
                    return True
                return False
        except Exception:
            return False


def run_search_task(task: dict, emit) -> dict:
    """执行一个搜索任务：搜索 → 归一化初筛 → 并行探活，确认一首上交一首。

    emit(entry) 的 entry 是 [item, download_headers, lyric]（JSON 行协议里的
    数组形态）。返回 {"count": n, "partial": bool}；partial=True 表示探活
    没在预算内跑完（父进程收到后不复用本 worker）。
    """
    source = str(task.get("source") or "")
    keyword = str(task.get("keyword") or "")
    fetch_size = max(1, int(task.get("fetch_size") or 10))
    limit = max(1, int(task.get("limit") or 1))
    song_id = task.get("song_id") or None
    budget_s = float(task.get("budget_s") or 0)
    deadline = time.monotonic() + budget_s

    if deadline is not None and time.monotonic() >= deadline:
        return {"count": 0, "partial": True}
    songs = search_one_source(source, keyword, fetch_size)
    candidates = []
    for song in songs[:fetch_size]:
        if not song:
            continue
        item = normalize(song, keyword)
        if song_id is not None and item["id"] != song_id:
            continue
        if not is_candidate_playable(song, item):
            continue
        headers = getattr(song, "default_download_headers", {}) or {}
        lyric = str(getattr(song, "lyric", "") or "")
        candidates.append((item, headers, lyric))
    if not candidates:
        return {"count": 0, "partial": False}
    if deadline is not None and time.monotonic() >= deadline:
        # 预算已在搜索阶段耗尽：不再起探活，交白卷并标记 partial
        return {"count": 0, "partial": True}

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
