"""musicdl-service 鲁棒性组件：缓存、源熔断器、进程隔离舱壁、SingleFlight 并发去重.

纯标准库实现，零第三方依赖，线程安全。
"""
import asyncio
from collections import OrderedDict
from contextlib import suppress
import inspect
import logging
import uuid
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, List, Optional


class SourceBusy(RuntimeError):
    """A source already has work running; callers must not enqueue more."""


class WorkerDead(RuntimeError):
    """worker 进程异常退出或协议流损坏；坑位已回收，下一次请求自动重建。"""


class WorkerTaskError(RuntimeError):
    """任务级失败：worker 上报后仍健康存活，坑位保留复用，仅本次任务失败。"""


class SearchProgress:
    """Bounded per-request handoff, never a cache; close rejects late results."""

    def __init__(self, limit, deadline):
        self.limit = limit
        self.deadline = deadline
        self._entries = []
        self.partial = False
        self._closed = False
        self._lock = threading.Lock()

    def stopped(self):
        with self._lock:
            return self._closed or time.monotonic() >= self.deadline

    def append(self, entry):
        with self._lock:
            if not self._closed and time.monotonic() < self.deadline and len(self._entries) < self.limit:
                self._entries.append(entry)

    def finish(self):
        with self._lock:
            self._closed = True
            return list(self._entries)


_WORKER_SCRIPT = Path(__file__).resolve().parent / "worker.py"


async def spawn_subprocess_worker(source, on_worker_line=None, script=None):
    """启动一个 worker 子进程（当前解释器），返回父进程侧句柄。

    不从多线程服务直接 fork；PYTHONPATH 前置脚本目录保证 search_core 可导入。
    """
    script = Path(script or _WORKER_SCRIPT)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(script.parent) + (os.pathsep + existing if existing else "")
    # Shield creation: cancellation may arrive after exec but before the handle is returned.
    starting = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, "-u", str(script),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env, limit=4 * 1024 * 1024,
    ))
    try:
        proc = await asyncio.shield(starting)
    except asyncio.CancelledError:
        async def recover():
            proc = await starting
            await _SubprocessWorker(proc, source).terminate()
        cleanup = asyncio.create_task(recover())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
        # 待处理的取消必须原样传播：清理失败只降级为吞掉，不得顶替 CancelledError。
        with suppress(Exception):
            cleanup.result()
        raise
    worker = _SubprocessWorker(proc, source, on_worker_line)
    worker.start_stderr_pump()
    return worker


class _SubprocessWorker:
    """worker 子进程的父进程侧句柄。

    协议流走独立 stdout，库输出改道 stderr 并丢弃；一次只执行一个任务。
    """

    def __init__(self, proc, source, on_worker_line=None):
        self.proc = proc
        self.source = source
        self.ready = False
        self._on_worker_line = on_worker_line
        self._stderr_task = None

    def start_stderr_pump(self):
        if self.proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._pump_stderr())

    async def _pump_stderr(self):
        # Arbitrary library output may contain credentials, lyrics or URLs.
        # Drain bounded chunks and deliberately discard it, even without a logger.
        while await self.proc.stderr.read(8192):
            pass

    def alive(self) -> bool:
        return self.proc.returncode is None

    async def wait_ready(self, timeout: float) -> None:
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError("worker startup exceeded budget") from None
        if not line:
            raise WorkerDead("worker exited before ready handshake")
        try:
            msg = json.loads(line)
        except ValueError as exc:
            raise WorkerDead("corrupt worker handshake") from None
        if not isinstance(msg, dict) or msg.get("type") != "ready":
            raise WorkerDead("unexpected worker handshake")
        self.ready = True

    async def execute(self, payload: dict, on_entry, deadline: float) -> dict:
        """执行一个任务直到 result/error；确认条目经 on_entry 即时上交。

        超时抛 asyncio.TimeoutError；进程死亡/流损坏抛 WorkerDead；
        任务级失败（worker 仍健康，可复用）抛 WorkerTaskError。
        """
        if deadline - time.monotonic() <= 0:
            raise asyncio.TimeoutError("worker task budget already exhausted")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise WorkerDead(f"worker stdin closed: {exc}") from None
        entries = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError("worker task budget exhausted")
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError("worker task budget exhausted") from None
            if not line:
                raise WorkerDead("worker exited mid-task")
            try:
                msg = json.loads(line)
            except ValueError:
                raise WorkerDead("corrupt worker output") from None
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "entry":
                entry = msg.get("entry")
                if (not isinstance(entry, list) or len(entry) != 3
                        or not isinstance(entry[0], dict) or not isinstance(entry[0].get("id"), str)
                        or not isinstance(entry[1], dict) or not isinstance(entry[2], str)):
                    raise WorkerDead("invalid worker entry")
                entries.append(entry)
                if on_entry is not None:
                    on_entry(entry)
            elif kind == "result":
                if type(msg.get("count")) is not int or type(msg.get("partial")) is not bool:
                    raise WorkerDead("invalid worker result")
                if msg["count"] != len(entries):
                    raise WorkerDead("worker entry count mismatch")
                return {"entries": entries,
                        "count": int(msg.get("count") or len(entries)),
                        "partial": bool(msg.get("partial"))}
            elif kind == "error":
                raise WorkerTaskError(str(msg.get("error") or "worker task failed"))
            else:
                raise WorkerDead("unknown worker message")

    async def terminate(self, grace_s: float = 0.5) -> None:
        """TERM, bounded grace, KILL, then unconditionally confirm reap."""
        if self.proc.returncode is None:
            with suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), min(0.5, grace_s))
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    self.proc.kill()
        await self.proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task


class _SourceSlot:
    __slots__ = ("busy", "worker", "stats")

    def __init__(self):
        self.busy = False
        self.worker = None
        self.stats = {"admitted": 0, "ok": 0, "timeouts": 0, "crashed": 0, "task_failed": 0,
                      "recycled": 0, "last_recycle_reason": ""}


class SourceBulkhead:
    """每源一个受管 worker 子进程：一次一任务，按需启动，干净完成后复用。

    执行体在子进程内（默认 worker.py；spawn 仅作为显式测试边界），卡死的
    任务不再永久占用音源：超时、取消或聚合提前结束时回收 worker——先 TERM，
    最多 term_grace_s 秒，再 KILL，确认退出后才释放坑位。排队等待、进程启动、
    搜索与探活共用调用方传入的单个超时预算；预算耗尽 admission 抢不到时抛
    SourceBusy（busy 等待与主动跳过不是上游失败，由调用方决定计分）。
    """

    def __init__(self, sources, spawn=None,
                 term_grace_s: float = 0.5, on_worker_line=None) -> None:
        self._slots = {source: _SourceSlot() for source in set(sources)}
        self._spawn = spawn
        self._term_grace_s = min(0.5, max(0.0, float(term_grace_s)))
        self._on_worker_line = on_worker_line
        self._lock = threading.Lock()
        self._closed = False
        self._tasks = set()

    async def run(self, source, payload: dict, *, timeout: float, on_entry=None) -> dict:
        slot = self._slots.get(source)
        if slot is None:
            raise ValueError(f"unknown source {source!r}")
        deadline = time.monotonic() + max(0, float(timeout))
        # busy 的源意味着上一个关键词的任务还占着坑。立即拒绝会让调用方拿到
        # 空结果而真实结果仍在路上，所以在调用方自己的预算内等待 admission。
        while True:
            with self._lock:
                if self._closed:
                    raise SourceBusy("source workers shutting down")
                if not slot.busy:
                    slot.busy = True
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceBusy("source busy (previous search still running); retry later")
            await asyncio.sleep(min(0.02, remaining))
        self._tasks.add(asyncio.current_task())
        slot.stats["admitted"] += 1
        task_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        phase = "start"
        reason = "complete"
        count = 0

        def confirmed(entry):
            nonlocal count
            count += 1
            if on_entry is not None:
                on_entry(entry)

        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                worker = await self._ensure_worker(source, slot, deadline)
                phase = "search_probe"
                remaining = max(0, deadline - time.monotonic())
                payload = dict(payload, task_id=task_id,
                               budget_s=max(0, remaining - min(0.1, remaining * 0.1)))
                result = await worker.execute(payload, confirmed, deadline)
                count = result.get("count", 0)
            if result.get("partial"):
                reason = "pending_probes"
                await self._recycle(slot, worker, reason)
            else:
                slot.stats["ok"] += 1
            return result
        except WorkerTaskError:
            # 任务级失败：worker 上报后仍健康（worker.py 协议），保留坑位复用，
            # 不回收不计崩溃；由调用方按源失败处理本次任务。
            reason = "task_failed"
            slot.stats["task_failed"] += 1
            raise
        except BaseException as exc:
            reason = ("cancelled" if isinstance(exc, asyncio.CancelledError) else
                      "timeout" if isinstance(exc, asyncio.TimeoutError) else "worker_failed")
            slot.stats["timeouts" if reason == "timeout" else "crashed"] += 1
            if slot.worker is not None:
                await self._recycle(slot, slot.worker, reason)
            raise
        finally:
            logging.getLogger("musicdl_service").info(
                "worker task=%s source=%s phase=%s duration=%.3f count=%d recycle=%s",
                task_id, source, phase, time.monotonic() - started, count, reason)
            self._tasks.discard(asyncio.current_task())
            with self._lock:
                slot.busy = False

    async def _ensure_worker(self, source, slot, deadline: float):
        worker = slot.worker
        if worker is not None and worker.ready and worker.alive():
            return worker
        if worker is not None:
            await self._recycle(slot, worker, "dead or unready worker")
        if deadline - time.monotonic() <= 0.05:
            raise asyncio.TimeoutError("worker startup budget exhausted")
        spawn = self._spawn or spawn_subprocess_worker
        worker = await spawn(source, on_worker_line=self._on_worker_line)
        slot.worker = worker
        if self._closed:
            raise SourceBusy("source workers shutting down")
        await worker.wait_ready(max(0, deadline - time.monotonic()))
        return worker

    async def _recycle(self, slot, worker, reason: str) -> None:
        slot.stats["recycled"] += 1
        slot.stats["last_recycle_reason"] = str(reason)[:200]
        # Repeated caller cancellation must never release admission ahead of reap.
        cleanup = asyncio.create_task(worker.terminate(self._term_grace_s))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if slot.worker is worker:
            slot.worker = None
        if cancelled:
            raise asyncio.CancelledError

    def stats(self) -> dict:
        with self._lock:
            sources = {}
            for name, slot in sorted(self._slots.items()):
                worker = slot.worker
                if slot.busy:
                    state = "busy"
                elif worker is not None and worker.ready and worker.alive():
                    state = "idle"
                else:
                    state = "down"
                entry = dict(slot.stats)
                entry["state"] = state
                sources[name] = entry
            return {
                "sources": sources,
                "totals": {
                    "admitted": sum(s["admitted"] for s in sources.values()),
                    "ok": sum(s["ok"] for s in sources.values()),
                    "timeouts": sum(s["timeouts"] for s in sources.values()),
                    "crashed": sum(s["crashed"] for s in sources.values()),
                    "task_failed": sum(s["task_failed"] for s in sources.values()),
                    "recycled": sum(s["recycled"] for s in sources.values()),
                },
                "term_grace_s": self._term_grace_s,
            }

    async def aclose(self) -> None:
        """Stop active admissions/startup first, then reap every idle process."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for slot in self._slots.values():
            if slot.worker is not None:
                await self._recycle(slot, slot.worker, "shutdown")

    def shutdown(self, wait=True):
        """Only valid without real workers; async process owners must await aclose."""
        if any(getattr(slot.worker, "proc", None) is not None for slot in self._slots.values()):
            raise RuntimeError("subprocess workers require await aclose()")
        self._closed = True


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
    """单个音源的熔断器（连续失败达到阈值开启熔断，cooldown 后自动半开/关闭）。

    可选慢响应降级：配置 slow_threshold_s 后，耗时超过该阈值的"成功"响应
    也计入连续失败，持续慢的源最终会被熔断，避免长期拖慢全局搜索。
    """

    def __init__(
        self,
        failure_threshold: int = 4,
        cooldown: int = 120,
        slow_threshold_s: Optional[float] = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.slow_threshold_s = slow_threshold_s
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, source: str, latency: Optional[float] = None) -> None:
        with self._lock:
            if (
                self.slow_threshold_s is not None
                and latency is not None
                and latency > self.slow_threshold_s
            ):
                # 慢成功视为降级信号：计入失败而不是重置
                count = self._failures.get(source, 0) + 1
                self._failures[source] = count
                if count >= self.failure_threshold and source not in self._opened_at:
                    self._opened_at[source] = time.time()
                return
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


class AdaptiveTimeout:
    """单源自适应超时：连续超时/慢响应的源逐步收紧超时，成功后逐步恢复。

    - 超时或慢响应一次：penalty *= shrink_factor（超时被压到 min_timeout 下限）
    - 正常成功一次：penalty *= recover_factor（逐步回到 base_timeout）
    目的：慢源每次阻塞的时间越来越短，直至触发 SourceBreaker 熔断。
    """

    def __init__(
        self,
        base_timeout: float = 12.0,
        min_timeout: float = 3.0,
        shrink_factor: float = 0.6,
        recover_factor: float = 1.25,
        slow_latency: float = 8.0,
    ) -> None:
        self.base_timeout = base_timeout
        self.min_timeout = min_timeout
        self.shrink_factor = shrink_factor
        self.recover_factor = recover_factor
        self.slow_latency = slow_latency
        self._penalty: dict[str, float] = {}
        self._lock = threading.Lock()

    def _floor(self) -> float:
        return self.min_timeout / self.base_timeout if self.base_timeout > 0 else 0.0

    def timeout_for(self, source: str) -> float:
        with self._lock:
            return max(self.min_timeout, self.base_timeout * self._penalty.get(source, 1.0))

    def record_success(self, source: str, latency: float = 0.0) -> None:
        with self._lock:
            penalty = self._penalty.get(source)
            if latency > 0 and latency > self.slow_latency:
                new = (penalty if penalty is not None else 1.0) * self.shrink_factor
            elif penalty is not None:
                new = min(1.0, penalty * self.recover_factor)
            else:
                return
            self._penalty[source] = max(self._floor(), min(1.0, new))

    def record_failure(self, source: str) -> None:
        with self._lock:
            cur = self._penalty.get(source, 1.0)
            self._penalty[source] = max(self._floor(), cur * self.shrink_factor)

    def stats(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                src: {
                    "timeout": round(max(self.min_timeout, self.base_timeout * p), 2),
                    "penalty": round(p, 3),
                }
                for src, p in sorted(self._penalty.items())
            }


class SingleFlight:
    """Shared task ownership is independent of the first waiter."""

    def __init__(self):
        self._inflight = {}
        self._waiters = {}

    async def run(self, key, fn):
        task = self._inflight.get(key)
        if task is None:
            async def invoke():
                value = fn() if callable(fn) else fn
                return await value if inspect.isawaitable(value) else value
            task = asyncio.create_task(invoke())
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            self._inflight[key] = task
            self._waiters[key] = 0
        self._waiters[key] += 1
        try:
            return await asyncio.shield(task)
        finally:
            self._waiters[key] -= 1
            if not self._waiters[key]:
                self._inflight.pop(key, None)
                self._waiters.pop(key, None)
                if not task.done():
                    task.cancel()
                    cleanup = asyncio.gather(task, return_exceptions=True)
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            pass

    async def aclose(self):
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
