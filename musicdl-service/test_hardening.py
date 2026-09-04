import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

# 确保能 import 同目录下的 hardening
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hardening import SearchCache, SourceBreaker, SingleFlight


class TestSearchCache:
    def test_cache_put_and_get_hit(self):
        cache = SearchCache(ttl=60, max_entries=10)
        items = [{"id": "migu:123", "title": "Song A"}]
        cache.put("周杰伦", "migu,bilibili", items)

        cached = cache.get("周杰伦", "migu,bilibili")
        assert cached == items
        # 确保返回的是独立列表副本
        assert cached is not items

    def test_cache_miss_and_key_isolation(self):
        cache = SearchCache(ttl=60, max_entries=10)
        items = [{"id": "migu:123", "title": "Song A"}]
        cache.put("周杰伦", "migu,bilibili", items)

        # 关键词不同
        assert cache.get("林俊杰", "migu,bilibili") is None
        # 源组合不同
        assert cache.get("周杰伦", "migu") is None

    def test_cache_ttl_expiration(self, monkeypatch):
        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        cache = SearchCache(ttl=10, max_entries=10)
        items = [{"id": "1", "title": "Song 1"}]
        cache.put("keyword", "src", items)

        # 5 秒后未过期
        current_time = 1005.0
        assert cache.get("keyword", "src") == items
        assert len(cache) == 1

        # 11 秒后已过期
        current_time = 1011.0
        assert cache.get("keyword", "src") is None
        assert len(cache) == 0

    def test_cache_custom_ttl(self):
        cache = SearchCache(ttl=300, max_entries=10)
        cache.put("normal_key", "src", [{"id": "1"}])  # 默认 ttl 300 对照
        cache.put("short_key", "src", [], ttl=1)       # 自定义 ttl 1

        assert cache.get("short_key", "src") == []
        assert cache.get("normal_key", "src") == [{"id": "1"}]

        time.sleep(1.2)

        assert cache.get("short_key", "src") is None
        assert cache.get("normal_key", "src") == [{"id": "1"}]

    def test_cache_max_entries_eviction(self):
        cache = SearchCache(ttl=60, max_entries=3)
        cache.put("k1", "src", [{"id": "1"}])
        cache.put("k2", "src", [{"id": "2"}])
        cache.put("k3", "src", [{"id": "3"}])

        assert len(cache) == 3

        # 写入第 4 个，最旧的 k1 应该被淘汰
        cache.put("k4", "src", [{"id": "4"}])
        assert len(cache) == 3
        assert cache.get("k1", "src") is None
        assert cache.get("k2", "src") == [{"id": "2"}]
        assert cache.get("k3", "src") == [{"id": "3"}]
        assert cache.get("k4", "src") == [{"id": "4"}]

    def test_cache_lru_access_order(self):
        cache = SearchCache(ttl=60, max_entries=3)
        cache.put("k1", "src", [{"id": "1"}])
        cache.put("k2", "src", [{"id": "2"}])
        cache.put("k3", "src", [{"id": "3"}])

        # 访问 k1，使 k1 成为最新访问，k2 变成最旧
        assert cache.get("k1", "src") == [{"id": "1"}]

        # 插入 k4，k2 应该被淘汰
        cache.put("k4", "src", [{"id": "4"}])
        assert cache.get("k2", "src") is None
        assert cache.get("k1", "src") == [{"id": "1"}]
        assert cache.get("k3", "src") == [{"id": "3"}]
        assert cache.get("k4", "src") == [{"id": "4"}]

    def test_cache_clear(self):
        cache = SearchCache(ttl=60, max_entries=10)
        cache.put("k1", "src", [{"id": "1"}])
        cache.put("k2", "src", [{"id": "2"}])
        assert len(cache) == 2
        cache.clear()
        assert len(cache) == 0
        assert cache.get("k1", "src") is None

    def test_cache_thread_safety(self):
        cache = SearchCache(ttl=60, max_entries=50)
        errors = []

        def worker(w_id: int):
            try:
                for i in range(100):
                    key = f"key_{w_id}_{i % 10}"
                    cache.put(key, "src", [{"id": f"{w_id}_{i}"}])
                    res = cache.get(key, "src")
                    if res is not None and not isinstance(res, list):
                        errors.append("Invalid result type")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(cache) <= 50


class TestSourceBreaker:
    def test_breaker_initial_state(self):
        breaker = SourceBreaker(failure_threshold=4, cooldown=120)
        assert not breaker.is_open("migu")
        assert breaker.get_open_sources() == []

    def test_breaker_threshold_trigger(self):
        breaker = SourceBreaker(failure_threshold=4, cooldown=120)
        for _ in range(3):
            breaker.record_failure("migu")
            assert not breaker.is_open("migu")

        # 第 4 次失败，触发熔断
        breaker.record_failure("migu")
        assert breaker.is_open("migu")
        assert breaker.get_open_sources() == ["migu"]

    def test_breaker_cooldown_recovery(self, monkeypatch):
        current_time = 1000.0
        monkeypatch.setattr(time, "time", lambda: current_time)

        breaker = SourceBreaker(failure_threshold=3, cooldown=60)
        for _ in range(3):
            breaker.record_failure("kuwo")
        assert breaker.is_open("kuwo")
        assert breaker.get_open_sources() == ["kuwo"]

        # 冷却时间内依然处于熔断状态
        current_time = 1030.0
        assert breaker.is_open("kuwo")
        assert breaker.get_open_sources() == ["kuwo"]

        # 冷却时间后自动恢复
        current_time = 1061.0
        assert not breaker.is_open("kuwo")
        assert breaker.get_open_sources() == []

        # 恢复后失败计数已重置，单次失败不会立即熔断
        breaker.record_failure("kuwo")
        assert not breaker.is_open("kuwo")

    def test_breaker_record_success_resets(self):
        breaker = SourceBreaker(failure_threshold=3, cooldown=60)
        breaker.record_failure("migu")
        breaker.record_failure("migu")
        # 成功后重置计数
        breaker.record_success("migu")
        # 再失败两次仍不到 3 次
        breaker.record_failure("migu")
        breaker.record_failure("migu")
        assert not breaker.is_open("migu")

        # 达到 3 次熔断后，成功也能立即重置
        breaker.record_failure("migu")
        assert breaker.is_open("migu")
        breaker.record_success("migu")
        assert not breaker.is_open("migu")
        assert breaker.get_open_sources() == []

    def test_breaker_multiple_sources_isolation(self):
        breaker = SourceBreaker(failure_threshold=2, cooldown=60)
        breaker.record_failure("src_a")
        breaker.record_failure("src_a")

        assert breaker.is_open("src_a")
        assert not breaker.is_open("src_b")
        assert breaker.get_open_sources() == ["src_a"]

        breaker.record_failure("src_b")
        breaker.record_failure("src_b")
        assert breaker.get_open_sources() == ["src_a", "src_b"]

    def test_breaker_thread_safety(self):
        breaker = SourceBreaker(failure_threshold=10, cooldown=60)
        errors = []

        def worker(source: str):
            try:
                for _ in range(50):
                    breaker.record_failure(source)
                    _ = breaker.is_open(source)
                    breaker.record_success(source)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(f"src_{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestSingleFlight:
    def test_single_flight_dedup(self):
        async def main():
            sf = SingleFlight()
            call_count = 0

            async def mock_fn():
                nonlocal call_count
                call_count += 1
                await asyncio.sleep(0.05)
                return {"result": "ok"}

            t1 = asyncio.create_task(sf.run("k1", mock_fn))
            t2 = asyncio.create_task(sf.run("k1", mock_fn))

            res1, res2 = await asyncio.gather(t1, t2)
            assert res1 == {"result": "ok"}
            assert res2 == {"result": "ok"}
            assert call_count == 1

        asyncio.run(main())

    def test_single_flight_exception_cleanup(self):
        async def main():
            sf = SingleFlight()
            call_count = 0

            async def mock_fail():
                nonlocal call_count
                call_count += 1
                await asyncio.sleep(0.05)
                raise RuntimeError("something went wrong")

            t1 = asyncio.create_task(sf.run("k_fail", mock_fail))
            t2 = asyncio.create_task(sf.run("k_fail", mock_fail))

            res1 = await asyncio.gather(t1, t2, return_exceptions=True)
            assert isinstance(res1[0], RuntimeError)
            assert str(res1[0]) == "something went wrong"
            assert isinstance(res1[1], RuntimeError)
            assert str(res1[1]) == "something went wrong"
            assert call_count == 1

            # 验证 in-flight 已被清理，再次调用能够重新执行
            async def mock_success():
                nonlocal call_count
                call_count += 1
                return {"result": "recovered"}

            res3 = await sf.run("k_fail", mock_success)
            assert res3 == {"result": "recovered"}
            assert call_count == 2

        asyncio.run(main())
