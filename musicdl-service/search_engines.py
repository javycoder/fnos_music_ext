"""musicdl 搜索引擎层：统一搜索接口 + 旧版/新版两个实现。

- 旧版（legacy）：本项目自实现的多站聚合——熔断、自适应超时、一进程一源、
  整轮截止、逐首探活。从 app.py /search 的 _do_search 原样迁入，行为不变。
- 新版（official）：musicdl 官方聚合搜索。一次 MusicClient(music_sources=白名单)
  调用交给库自带聚合；limit 等官方不支持的参数直接忽略。仍在 worker 子进程内
  执行，父进程只保留「到点回收进程」的自保预算，不把超时/熔断参数传给 SDK。

两个引擎对 app 模块状态（worker 池、熔断器、缓存等）的依赖经 deps 在调用期
按名解析：app.py 传入自身 globals() 的动态视图，测试 monkeypatch app 模块
属性后引擎立即看到新值，不需要重启或重建实例。
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from hardening import SourceBusy, WorkerTaskError


OFFICIAL_WORKER_KEY = "official"


@dataclass(frozen=True)
class SearchRequest:
    """一次聚合搜索请求。sources 为已归一的 *MusicClient 全名，保持用户顺序。"""

    keyword: str
    sources: list
    limit: int


@dataclass
class SearchOutcome:
    """聚合结果：条目、按源错误与是否命中搜索缓存。"""

    items: list
    errors: dict
    cached: bool = False


class SearchEngine(Protocol):
    name: str

    def cache_key(self, request: SearchRequest) -> str: ...

    async def search(self, request: SearchRequest) -> SearchOutcome: ...


class AppGlobalsProxy:
    """app 模块命名空间的动态视图。

    引擎构造时持有本代理而非具体对象；属性在调用期解析，app 模块级对象
    （SOURCE_WORKERS / SOURCE_BREAKER / _cache_put 等）被测试替换后立即生效。
    """

    def __init__(self, ns: dict) -> None:
        object.__setattr__(self, "_ns", ns)

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "_ns")[name]
        except KeyError:
            raise AttributeError(name) from None


class LegacySearchEngine:
    """旧版：本项目自实现的聚合搜索（原 app.py /search._do_search 全量迁移）。"""

    name = "legacy"

    def __init__(self, deps: AppGlobalsProxy) -> None:
        self.deps = deps

    def cache_key(self, request: SearchRequest) -> str:
        sources_key = ",".join(sorted(request.sources))
        return f"{self.name}|{sources_key}|limit={request.limit}"

    async def search(self, request: SearchRequest) -> SearchOutcome:
        keyword, limit = request.keyword, request.limit
        raw_src_list = request.sources
        cache_key = self.cache_key(request)
        deps = self.deps

        # 1. 过滤熔断中的源
        active_src_list = [s for s in raw_src_list if not deps.SOURCE_BREAKER.is_open(s)]

        # 2. 查缓存（命中直接返回）
        cached = deps.SEARCH_CACHE.get(keyword, cache_key)
        # Search results alone cannot serve playback after song-cache eviction.
        if cached is not None and all(deps._cache_get(item["id"]) for item in cached):
            return SearchOutcome(items=cached, errors={}, cached=True)

        # 全被熔断且无缓存，直接返回空结果（不报错）
        if not active_src_list:
            return SearchOutcome(
                items=[], errors={s: "circuit breaker open" for s in raw_src_list})

        progress_by_source = {}

        def collect(source):
            progress = progress_by_source.get(source)
            items = []
            for item, headers, lyric in progress.finish() if progress else []:
                deps._cache_put(item, keyword, headers, lyric)
                items.append(item)
            return items

        async def one(source: str):
            timeout = deps.ADAPTIVE.timeout_for(source)
            started = time.monotonic()
            progress = deps.SearchProgress(limit, started + timeout)
            progress_by_source[source] = progress
            fetch_size = max(limit * 2, 10)
            payload = deps._search_payload(source, keyword, fetch_size, limit, None, timeout)

            def on_entry(entry):
                # worker 每确认一首即上交：[item, download_headers, lyric]
                progress.append((entry[0], entry[1], entry[2]))

            try:
                result = await deps.SOURCE_WORKERS.run(source, payload, timeout=timeout, on_entry=on_entry)
                progress.partial = bool(result.get("partial"))
                latency = time.monotonic() - started
                items = collect(source)
                if progress.partial:
                    # 已经交出歌就不要再把超时往下压。压到搜不完一页后，下次还是 0 条。
                    if not items:
                        deps.ADAPTIVE.record_failure(source)
                    return source, items, "partial results (probe deadline reached)"
                deps.ADAPTIVE.record_success(source, latency)
                if items:
                    deps.SOURCE_BREAKER.record_success(source, latency)
                else:
                    deps._note_empty_source_result(source, deps.SOURCE_BREAKER, deps.ADAPTIVE)
                return source, items, None
            except SourceBusy as exc:
                # Admission rejection is not another upstream failure.
                return source, collect(source), str(exc)
            except asyncio.TimeoutError:
                items = collect(source)
                if not items:
                    deps.SOURCE_BREAKER.record_failure(source)
                    deps.ADAPTIVE.record_failure(source)
                suffix = " (partial results)" if items else ""
                return source, items, f"timeout after {timeout:.1f}s{suffix}"
            except asyncio.CancelledError:
                # The aggregate owner collects this closed snapshot on the
                # global deadline, never the still-running worker.
                progress.finish()
                raise
            except Exception as e:  # 单源失败不影响其他源
                deps._STATS["errors"] += 1
                deps.SOURCE_BREAKER.record_failure(source)
                return source, collect(source), f"{type(e).__name__}: {e}"

        results: list = []
        slots = asyncio.Semaphore(max(1, int(deps.CONF.get("concurrency") or 1)))

        async def limited(source: str):
            # issue #116：排队时间不计入单源预算——拿到槽位后才开始它自己的超时，
            # 否则站点一多，后面排队的源必然在起跑线上就超时。
            async with slots:
                return await one(source)

        pending = {asyncio.create_task(limited(s), name=s): s for s in active_src_list}
        overall = max(float(deps.CONF["search_timeout"]), 4.0)
        deadline = time.monotonic() + overall
        try:
            # issue #116：只按整体截止收口，不再「凑够 N 条就砍掉其余源」或「有结果
            # 后再等固定几秒」——那正是勾选多站点却只剩约 10 首的原因。
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _ = await asyncio.wait(
                    pending.keys(), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    pending.pop(task, None)
                    try:
                        results.append(task.result())
                    except Exception as e:  # noqa: BLE001
                        deps._STATS["errors"] += 1
                        results.append((task.get_name(), [], f"{type(e).__name__}: {e}"))
        finally:
            for task in pending:
                task.cancel()
            if pending:
                cleanup = asyncio.gather(*pending, return_exceptions=True)
                interrupted = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        interrupted = True
                if interrupted:
                    raise asyncio.CancelledError
        if pending:
            # 整体截止时，已确认的歌照常并入；没排到槽位/没跑完的源记 skipped，
            # 一律不记熔断、不压自适应超时——否则站点选多了会把「还没来得及跑」
            # 的源逐个打成故障，下次搜索直接空结果（issue #116）。
            for _task, src in pending.items():
                items = collect(src)
                results.append((src, items,
                                "skipped (deadline)" + (" (partial results)" if items else "")))

        all_items = []
        seen_ids = set()
        for _, items, _ in results:
            for item in items:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    all_items.append(item)

        errors = {src: err for src, _, err in results if err}
        for s in raw_src_list:
            if s not in active_src_list:
                errors.setdefault(s, "circuit breaker open")
        # Partial/error results must remain retryable, not become a long-lived
        # apparently complete cache hit with errors silently removed.
        if not errors:
            if all_items:
                deps.SEARCH_CACHE.put(keyword, cache_key, all_items)
            else:
                deps.SEARCH_CACHE.put(keyword, cache_key, [], ttl=deps.CONF["neg_cache_ttl"])

        return SearchOutcome(items=all_items, errors=errors)


class OfficialSearchEngine:
    """新版：musicdl 官方聚合搜索（实验开关，不影响网易与洛雪源）。

    官方 search(keyword) 只接受关键词：limit、单站超时、熔断、并发槽都不是它的
    参数，一律忽略、绝不注入。父进程仅保留整体截止预算：worker 卡死时由
    SourceBulkhead 回收子进程，不把超时记成任何平台的熔断失败。
    """

    name = "official"

    def __init__(self, deps: AppGlobalsProxy) -> None:
        self.deps = deps

    def cache_key(self, request: SearchRequest) -> str:
        sources_key = ",".join(sorted(request.sources))
        return f"{self.name}|{sources_key}|limit={request.limit}"

    async def search(self, request: SearchRequest) -> SearchOutcome:
        keyword = request.keyword
        cache_key = self.cache_key(request)
        deps = self.deps

        cached = deps.SEARCH_CACHE.get(keyword, cache_key)
        if cached is not None and all(deps._cache_get(item["id"]) for item in cached):
            return SearchOutcome(items=cached, errors={}, cached=True)

        items: list = []

        def on_entry(entry):
            # worker 每确认一首即上交：[item, download_headers, lyric]
            item, headers, lyric = entry[0], entry[1], entry[2]
            deps._cache_put(item, keyword, headers, lyric)
            items.append(item)

        timeout = max(4.0, float(deps.CONF["search_timeout"]))
        payload = {
            "op": "official_search",
            "sources": list(request.sources),
            "keyword": keyword,
        }
        try:
            await deps.OFFICIAL_WORKERS.run(
                OFFICIAL_WORKER_KEY, payload, timeout=timeout, on_entry=on_entry)
        except asyncio.TimeoutError:
            # 卡死/超支的官方聚合：进程由 SourceBulkhead 回收，本次交空（可重试）。
            return SearchOutcome(items=[], errors={OFFICIAL_WORKER_KEY: "timeout"})
        except SourceBusy:
            return SearchOutcome(
                items=[], errors={OFFICIAL_WORKER_KEY: "busy (previous official search still running)"})
        except WorkerTaskError as exc:
            # 错误只保留类型与阶段（worker 协议已脱敏），不计任何平台熔断。
            return SearchOutcome(items=[], errors={OFFICIAL_WORKER_KEY: str(exc)})

        # 无错误才落搜索缓存：部分/失败结果必须可重试，不成长期缓存。
        if items:
            deps.SEARCH_CACHE.put(keyword, cache_key, items)
        else:
            deps.SEARCH_CACHE.put(keyword, cache_key, [], ttl=deps.CONF["neg_cache_ttl"])
        return SearchOutcome(items=items, errors={})
