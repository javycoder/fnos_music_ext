"""musicdl-service 鲁棒性组件：缓存、源熔断器、SingleFlight 并发去重.

纯标准库实现，零第三方依赖，线程安全。
"""
import asyncio
from collections import OrderedDict
import inspect
import threading
import time
from typing import Any, Callable, List, Optional


class SearchCache:
    """搜索结果缓存（OrderedDict 实现 FIFO/LRU 淘汰与 TTL 过期）。"""

    def __init__(self, ttl: int = 300, max_entries: int = 200) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._cache: OrderedDict[tuple[str, str], tuple[list, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _make_key(self, keyword: str, sources_key: str) -> tuple[str, str]:
        return (keyword.strip(), sources_key.strip())

    def get(self, keyword: str, sources_key: str) -> Optional[List[dict]]:
        key = self._make_key(keyword, sources_key)
        with self._lock:
            if key not in self._cache:
                return None
            items, expires_at = self._cache[key]
            if time.time() > expires_at:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return list(items)

    def put(
        self,
        keyword: str,
        sources_key: str,
        items: List[dict],
        ttl: Optional[int] = None,
    ) -> None:
        key = self._make_key(keyword, sources_key)
        effective_ttl = self.ttl if ttl is None else ttl
        expires_at = time.time() + effective_ttl
        with self._lock:
            if key in self._cache:
                self._cache.pop(key, None)
            elif len(self._cache) >= self.max_entries:
                self._cache.popitem(last=False)
            self._cache[key] = (list(items), expires_at)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            now = time.time()
            expired = [k for k, (_, exp) in self._cache.items() if now > exp]
            for k in expired:
                self._cache.pop(k, None)
            return len(self._cache)


class SourceBreaker:
    """单个音源的熔断器（连续失败达到阈值开启熔断，cooldown 后自动半开/关闭）。"""

    def __init__(self, failure_threshold: int = 4, cooldown: int = 120) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, source: str) -> None:
        with self._lock:
            self._failures[source] = 0
            self._opened_at.pop(source, None)

    def record_failure(self, source: str) -> None:
        with self._lock:
            count = self._failures.get(source, 0) + 1
            self._failures[source] = count
            if count >= self.failure_threshold and source not in self._opened_at:
                self._opened_at[source] = time.time()

    def is_open(self, source: str) -> bool:
        with self._lock:
            opened_time = self._opened_at.get(source)
            if opened_time is None:
                return False
            now = time.time()
            if now - opened_time < self.cooldown:
                return True
            # 冷却时间已过，自动恢复
            self._failures[source] = 0
            self._opened_at.pop(source, None)
            return False

    def get_open_sources(self) -> List[str]:
        with self._lock:
            now = time.time()
            open_list = []
            to_reset = []
            for src, opened_time in list(self._opened_at.items()):
                if now - opened_time < self.cooldown:
                    open_list.append(src)
                else:
                    to_reset.append(src)
            for src in to_reset:
                self._failures[src] = 0
                self._opened_at.pop(src, None)
            return sorted(open_list)


class SingleFlight:
    """同键并发请求去重（纯 asyncio 实现）。"""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}

    async def run(self, key: str, fn: Callable[..., Any]) -> Any:
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            if callable(fn):
                res = fn()
                if inspect.isawaitable(res):
                    res = await res
            elif inspect.isawaitable(fn):
                res = await fn
            else:
                res = fn
            if not fut.done():
                fut.set_result(res)
            return res
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)

