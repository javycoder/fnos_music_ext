"""Isolated LX source runtime management.

The shared lxserver remains responsible for native search/metadata APIs. This
manager owns the legacy ``users/source/_open`` files and runs exactly one user
script per lightweight Node worker process. The worker protocol is line-delimited
JSON over an inherited Unix socket; no public port is opened.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any
import uuid

import httpx

from source_cpu import (
    DEFAULT_MAX_GAP_S,
    DEFAULT_REQUIRED_DURATION_S,
    DEFAULT_SAMPLE_INTERVAL_S,
    DEFAULT_THRESHOLD_PERCENT,
    ProcCpuSample,
    SourceCpuMonitor,
    linux_monitoring_available,
    read_proc_stat,
)

logger = logging.getLogger("fnmusic.lxmusic.runtime")

PROTOCOL_VERSION = 1
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_INIT_FRAME_BYTES = 16 * 1024 * 1024
MAX_RESOLVE_FRAME_BYTES = 64 * 1024
DEFAULT_INIT_TIMEOUT_S = 15.0
DEFAULT_STOP_BUDGET_S = 5.0
DEFAULT_IMPORT_TIMEOUT_S = 20.0
SOURCE_ID_RE = re.compile(r"^[^\\/\x00-\x1f]{1,240}\.js$")


class SourceRuntimeError(RuntimeError):
    """Controlled runtime error; ``code`` is safe to expose, detail is not."""

    def __init__(self, code: str, stage: str = "source_runtime"):
        self.code = str(code)
        self.stage = str(stage)
        super().__init__(self.code)


class SourceNotFound(SourceRuntimeError):
    def __init__(self, source_id: str = ""):
        self.source_id = source_id
        super().__init__("source_not_found", "source_state")


class SourceAlreadyExists(SourceRuntimeError):
    def __init__(self, source_id: str = ""):
        self.source_id = source_id
        super().__init__("source_already_exists", "source_import")


class SourceUnsafeVmRequired(SourceRuntimeError):
    def __init__(self):
        super().__init__("unsafe_vm_required", "source_init")


class SourceInitializationFailed(SourceRuntimeError):
    def __init__(self, code: str = "initialization_failed"):
        super().__init__(code, "source_init")


class SourceProcessUnavailable(SourceRuntimeError):
    def __init__(self, code: str = "source_process_unavailable"):
        super().__init__(code, "resolve")


class SourceProcessBusy(SourceRuntimeError):
    def __init__(self):
        super().__init__("source_process_busy", "resolve")


class SourceResolveTimeout(SourceRuntimeError):
    def __init__(self):
        super().__init__("resolve_timeout", "resolve")


class SourceScriptFailed(SourceRuntimeError):
    def __init__(self, code: str = "source_script_failed"):
        super().__init__(code, "request")


@dataclass
class SourceScriptMetadata:
    name: str
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    homepage: str = ""


def parse_script_metadata(script: str, fallback_name: str = "custom_source") -> SourceScriptMetadata:
    """Mirror lxserver's header-comment metadata contract."""

    meta = SourceScriptMetadata(name=fallback_name)
    block = re.search(r"/\*[*!]([\s\S]*?)\*/", str(script or ""))
    if not block:
        return meta
    values: dict[str, str] = {}
    for match in re.finditer(r"@(name|description|version|author|repository|homepage)\s+(.+)", block.group(1)):
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key in ("repository", "homepage"):
            values.setdefault("homepage", value)
        else:
            values.setdefault(key, value)
    return SourceScriptMetadata(
        name=values.get("name") or fallback_name,
        version=values.get("version") or "1.0.0",
        author=values.get("author") or "",
        description=values.get("description") or "",
        homepage=values.get("homepage") or "",
    )


def derive_source_id(name: str, filename: str = "") -> str:
    """Derive the same readable stable filename used by lxserver."""

    raw = str(name or filename or "source")
    try:
        from urllib.parse import unquote

        raw = unquote(raw)
    except (ValueError, UnicodeError):
        pass
    base = Path(raw.replace("\\", "/")).name
    if base.lower().endswith(".js"):
        base = base[:-3]
    clean = re.sub(r'[\\/:*?"<>|]', "_", base).strip()
    return f"{clean or 'source'}.js"


def _validate_source_id(source_id: str) -> str:
    sid = str(source_id or "").strip()
    if not sid or source_id != sid or not SOURCE_ID_RE.fullmatch(sid):
        raise SourceNotFound(sid)
    return sid


def _atomic_write(path: Path, data: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        with suppress(OSError):
            temp_path.unlink()
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return default


def _safe_error_code(value: Any, fallback: str) -> str:
    code = str(value or fallback).strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
        return fallback
    return code


class SourceProcess:
    """One initialized Node worker with a bounded request/response protocol."""

    def __init__(
        self,
        *,
        source_id: str,
        runtime_id: str,
        script_hash: str,
        process: asyncio.subprocess.Process,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        start_time_ticks: int,
        metadata: dict,
        platforms: list[str],
        qualities: dict[str, list[str]],
    ):
        self.source_id = source_id
        self.runtime_id = runtime_id
        self.script_hash = script_hash
        self.process = process
        self.reader = reader
        self.writer = writer
        self.start_time_ticks = int(start_time_ticks)
        self.metadata = dict(metadata or {})
        self.platforms = list(platforms or [])
        self.qualities = {
            str(key): [str(item) for item in value]
            for key, value in (qualities or {}).items()
            if isinstance(value, list)
        }
        self.status = "success"
        self.error = ""
        self.blocked = False
        self.cancel_unconfirmed = False
        self.stopping = False
        self.exited = False
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._close_waiters: dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self.active_request_id = ""

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def start_reader(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                if len(line) > MAX_INIT_FRAME_BYTES:
                    self.blocked = True
                    self.error = "protocol_frame_too_large"
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeError, ValueError):
                    self.blocked = True
                    self.error = "invalid_worker_message"
                    break
                if not isinstance(message, dict):
                    continue
                request_id = str(message.get("request_id") or "")
                runtime_id = str(message.get("runtime_id") or "")
                source_id = str(message.get("source_id") or "")
                if message.get("type") == "cancel_ack":
                    waiter = self._close_waiters.get(request_id)
                    if waiter is None:
                        continue
                    self._close_waiters.pop(request_id, None)
                    if waiter is not None and not waiter.done():
                        if runtime_id == self.runtime_id and source_id == self.source_id:
                            waiter.set_result(message)
                        else:
                            waiter.set_exception(SourceProcessUnavailable("runtime_identity_mismatch"))
                    continue
                if message.get("type") == "settled":
                    if runtime_id == self.runtime_id and source_id == self.source_id:
                        self.blocked = False
                        self.cancel_unconfirmed = False
                        if self.status == "failed" and self.error == "source_process_blocked":
                            self.status = "success"
                            self.error = ""
                    continue
                pending = self._pending.pop(request_id, None)
                if pending is None or pending.done():
                    continue
                if runtime_id != self.runtime_id or source_id != self.source_id:
                    pending.set_exception(SourceProcessUnavailable("runtime_identity_mismatch"))
                    continue
                pending.set_result(message)
        except (asyncio.CancelledError, OSError, ValueError):
            pass
        finally:
            self.exited = True
            for pending in list(self._pending.values()):
                if not pending.done():
                    pending.set_exception(SourceProcessUnavailable("source_process_exited"))
            self._pending.clear()
            for waiter in list(self._close_waiters.values()):
                if not waiter.done():
                    waiter.set_exception(SourceProcessUnavailable("source_process_exited"))
            self._close_waiters.clear()
            if self.status != "failed":
                self.status = "failed"
                self.error = self.error or "source_process_exited"

    async def send(
        self,
        message: dict,
        *,
        limit: int = MAX_RESOLVE_FRAME_BYTES,
        allow_stopping: bool = False,
    ) -> bool:
        if self.exited or (self.stopping and not allow_stopping):
            raise SourceProcessUnavailable("source_process_unavailable")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > limit:
            raise SourceScriptFailed("request_too_large")
        async with self._write_lock:
            try:
                self.writer.write(payload)
                await self.writer.drain()
            except (ConnectionError, OSError, RuntimeError) as exc:
                self.exited = True
                raise SourceProcessUnavailable("source_process_unavailable") from exc
        return True

    def prepare_request(self, request_id: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        return future

    async def wait_request(
        self,
        request_id: str,
        deadline: float,
        *,
        future: asyncio.Future | None = None,
    ) -> dict:
        if future is None:
            future = self.prepare_request(request_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._pending.pop(request_id, None)
            raise SourceResolveTimeout()
        try:
            return await asyncio.wait_for(asyncio.shield(future), remaining)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise SourceResolveTimeout() from exc
        finally:
            if self._pending.get(request_id) is future and not future.done():
                self._pending.pop(request_id, None)

    async def cancel(
        self,
        request_id: str,
        *,
        wait_s: float = 0.75,
        allow_stopping: bool = False,
    ) -> bool:
        waiter = asyncio.get_running_loop().create_future()
        self._close_waiters[request_id] = waiter
        try:
            await self.send({
                "protocol": PROTOCOL_VERSION,
                "type": "cancel",
                "request_id": request_id,
                "runtime_id": self.runtime_id,
                "source_id": self.source_id,
            }, allow_stopping=allow_stopping)
        except SourceRuntimeError:
            if self._close_waiters.get(request_id) is waiter:
                self._close_waiters.pop(request_id, None)
            return False
        try:
            await asyncio.wait_for(asyncio.shield(waiter), max(0.01, wait_s))
            return True
        except (TimeoutError, SourceRuntimeError):
            return False
        finally:
            if self._close_waiters.get(request_id) is waiter:
                self._close_waiters.pop(request_id, None)

    async def resolve(
        self,
        *,
        platform: str,
        song_info: dict,
        quality: str,
        deadline: float,
    ) -> dict:
        if self.exited or self.process.returncode is not None:
            raise SourceProcessUnavailable("source_process_exited")
        if self.blocked or self.cancel_unconfirmed:
            raise SourceProcessBusy()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceResolveTimeout()
        request_id = uuid.uuid4().hex
        waiter = self.prepare_request(request_id)
        self.active_request_id = request_id
        try:
            await self.send({
                "protocol": PROTOCOL_VERSION,
                "type": "resolve",
                "request_id": request_id,
                "runtime_id": self.runtime_id,
                "source_id": self.source_id,
                "platform": str(platform),
                "quality": str(quality),
                "song_info": song_info,
                "budget_ms": max(1, min(300_000, int((deadline - time.monotonic()) * 1000))),
            })
            try:
                message = await self.wait_request(request_id, deadline, future=waiter)
            except SourceResolveTimeout:
                confirmed = await self.cancel(
                    request_id,
                    wait_s=min(0.75, max(0.05, deadline - time.monotonic())),
                )
                if not confirmed:
                    self.cancel_unconfirmed = True
                    self.blocked = True
                    self.status = "failed"
                    self.error = "source_process_blocked"
                raise
            except asyncio.CancelledError:
                with suppress(Exception):
                    await self.cancel(request_id, wait_s=0.25)
                raise
        finally:
            if self.active_request_id == request_id:
                self.active_request_id = ""
            if self._pending.get(request_id) is waiter:
                self._pending.pop(request_id, None)
        if message.get("type") == "result" and message.get("ok") is True:
            result = message.get("result")
            if not isinstance(result, dict) or not str(result.get("url") or "").startswith(("http://", "https://")):
                raise SourceScriptFailed("source_returned_no_url")
            return result
        code = _safe_error_code(message.get("code"), "source_script_failed")
        if code == "source_process_busy":
            raise SourceProcessBusy()
        if code in ("resolve_timeout", "ipc_buffer_overflow"):
            self.blocked = True
            self.status = "failed"
            self.error = "source_process_blocked"
            raise SourceResolveTimeout()
        if code in ("runtime_identity_mismatch", "source_not_initialized"):
            raise SourceProcessUnavailable(code)
        raise SourceScriptFailed(code)

    async def stop(self, budget_s: float = DEFAULT_STOP_BUDGET_S) -> bool:
        """Stop this exact child with a bounded graceful/TERM/KILL sequence."""

        if self.process.returncode is not None:
            self.exited = True
            self._close_transport()
            return True
        deadline = time.monotonic() + max(0.05, float(budget_s))
        active_request_id = self.active_request_id
        if active_request_id:
            await self.cancel(
                active_request_id,
                wait_s=min(0.25, max(0.01, deadline - time.monotonic())),
                allow_stopping=True,
            )
        self.stopping = True
        self.blocked = True
        request_id = uuid.uuid4().hex
        try:
            await asyncio.wait_for(self.send({
                "protocol": PROTOCOL_VERSION,
                "type": "shutdown",
                "request_id": request_id,
                "runtime_id": self.runtime_id,
                "source_id": self.source_id,
            }, allow_stopping=True), timeout=min(0.25, max(0.01, deadline - time.monotonic())))
        except (Exception, asyncio.CancelledError):
            pass
        if await self._wait_exit(min(0.75, max(0.01, deadline - time.monotonic()))):
            self._close_transport()
            return True
        if not self._identity_ok():
            return False
        with suppress(ProcessLookupError, PermissionError):
            self.process.terminate()
        if await self._wait_exit(min(1.25, max(0.01, deadline - time.monotonic()))):
            self._close_transport()
            return True
        if not self._identity_ok():
            return False
        with suppress(ProcessLookupError, PermissionError):
            self.process.kill()
        ok = await self._wait_exit(min(1.5, max(0.01, deadline - time.monotonic())))
        self._close_transport()
        return ok

    async def _wait_exit(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self.process.wait(), max(0.01, timeout_s))
            return True
        except TimeoutError:
            return False

    def _identity_ok(self) -> bool:
        if self.process.returncode is not None:
            return False
        if not linux_monitoring_available():
            return True
        sample = read_proc_stat(self.pid)
        return bool(
            sample is not None
            and sample.start_time_ticks == self.start_time_ticks
        )

    def _close_transport(self) -> None:
        self.exited = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        with suppress(Exception):
            self.writer.close()


class SourceRuntimeManager:
    """Own source files, activation state, workers and CPU snapshots."""

    def __init__(
        self,
        *,
        source_root: str | Path | None = None,
        lxserver_dir: str | Path | None = None,
        node_bin: str | None = None,
        worker_path: str | Path | None = None,
        init_timeout_s: float = DEFAULT_INIT_TIMEOUT_S,
        stop_budget_s: float = DEFAULT_STOP_BUDGET_S,
        cpu_monitor: SourceCpuMonitor | None = None,
        monitor_interval_s: float | None = None,
    ):
        source_root = source_root or os.environ.get("LX_SOURCE_DATA_DIR") or os.environ.get(
            "LXSERVER_DATA_DIR", "/data/lxserver"
        )
        root = Path(source_root)
        if root.name != "_open":
            root = root / "users" / "source" / "_open"
        self.source_dir = root
        self.sources_path = root / "sources.json"
        self.order_path = root / "order.json"
        self.lxserver_dir = Path(
            lxserver_dir or os.environ.get("LXSERVER_DIR", "/srv/lxserver")
        )
        self.node_bin = str(node_bin or os.environ.get("FNMUSIC_NODE_BIN") or "node")
        self.worker_path = Path(worker_path or Path(__file__).resolve().parent / "source_worker.cjs")
        self.init_timeout_s = float(init_timeout_s)
        self.stop_budget_s = float(stop_budget_s)
        interval = monitor_interval_s
        if interval is None:
            try:
                interval = float(os.environ.get("LX_SOURCE_CPU_INTERVAL_S", DEFAULT_SAMPLE_INTERVAL_S))
            except ValueError:
                interval = DEFAULT_SAMPLE_INTERVAL_S
        self.monitor_interval_s = max(0.05, float(interval))
        self.cpu = cpu_monitor or SourceCpuMonitor(
            threshold_percent=self._env_float("LX_SOURCE_CPU_THRESHOLD", DEFAULT_THRESHOLD_PERCENT),
            required_duration_s=self._env_float("LX_SOURCE_CPU_REQUIRED_S", DEFAULT_REQUIRED_DURATION_S),
            sample_interval_s=self.monitor_interval_s,
            max_gap_s=max(DEFAULT_MAX_GAP_S, self.monitor_interval_s * 3),
        )
        self._lock = asyncio.Lock()
        self._runtimes: dict[str, SourceProcess] = {}
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._monitor_task: asyncio.Task | None = None
        self._closed = False
        self._started = False
        self._node_checked = False
        self._last_errors: dict[str, str] = {}

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return float(default)

    async def _stop_runtime(self, source_id: str, runtime: SourceProcess, budget_s: float) -> bool:
        self._runtimes.setdefault(source_id, runtime)
        try:
            stopped = await runtime.stop(budget_s)
        except Exception:
            stopped = False
        if stopped:
            if self._runtimes.get(source_id) is runtime:
                self._runtimes.pop(source_id, None)
            self.cpu.clear(runtime.runtime_id)
            return True
        runtime.blocked = True
        if linux_monitoring_available():
            self.cpu.register(runtime.runtime_id, runtime.pid, runtime.start_time_ticks)
        logger.warning("洛雪源进程未确认停止 source=%s", source_id)
        return False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._closed = False
            self._check_node()
            self.source_dir.mkdir(parents=True, exist_ok=True)
            records = self._load_records()
            enabled = [record for record in records if bool(record.get("enabled"))]
            for record in enabled:
                source_id = str(record.get("id") or "")
                try:
                    await self._start_record(record, deadline=time.monotonic() + self.init_timeout_s)
                except (SourceRuntimeError, OSError, ValueError) as exc:
                    self._last_errors[source_id] = self._error_code(exc)
                    logger.warning("洛雪源启动恢复失败 source=%s code=%s", source_id, self._error_code(exc))
            self._started = True
            if self._monitor_task is None and linux_monitoring_available():
                self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def close(self) -> None:
        if self._closed and not self._runtimes:
            return
        self._closed = True
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
        async with self._lock:
            runtimes = list(self._runtimes.items())
            if runtimes:
                await asyncio.gather(
                    *(self._stop_runtime(source_id, runtime, self.stop_budget_s)
                      for source_id, runtime in runtimes),
                    return_exceptions=True,
                )
        self._started = False

    async def list_sources(self) -> list[dict]:
        async with self._lock:
            records = self._load_records()
            known = {str(record.get("id") or "") for record in records}
            for source_id, runtime in list(self._runtimes.items()):
                if source_id in known:
                    continue
                await self._stop_runtime(source_id, runtime, self.stop_budget_s)
            result = [self._public_record(record) for record in records]
            result.sort(key=self._order_index)
            return result

    async def import_source(self, url: str, *, deadline: float) -> dict:
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            raise SourceRuntimeError("invalid_source_url", "source_import")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceResolveTimeout()
        timeout = max(0.01, min(DEFAULT_IMPORT_TIMEOUT_S, remaining))
        script = await self._download_script(target, timeout)
        result = await self.upload_source(
            self._filename_from_url(target),
            script,
            deadline=deadline,
            source_url=target,
        )
        return result

    async def upload_source(
        self,
        filename: str,
        content: str,
        *,
        deadline: float,
        source_url: str = "",
    ) -> dict:
        script = str(content or "")
        if not script.strip():
            raise SourceInitializationFailed("script_empty")
        if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise SourceInitializationFailed("script_too_large")
        async with self._lock:
            self.source_dir.mkdir(parents=True, exist_ok=True)
            records = self._load_records()
            fallback = Path(str(filename or "custom_source.js")).name or "custom_source.js"
            metadata = parse_script_metadata(script, fallback)
            source_id = _validate_source_id(derive_source_id(metadata.name, fallback))
            if any(str(record.get("id") or "") == source_id for record in records):
                raise SourceAlreadyExists(source_id)

            validation = await self._validate_script(source_id, script, deadline)
            script_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
            record = self._new_record(
                source_id,
                script,
                script_hash,
                validation,
                source_url=source_url,
                filename=fallback,
            )
            try:
                _atomic_write(self._source_path(source_id), script)
                records.append(record)
                self._save_records(records)
            except BaseException:
                with suppress(OSError):
                    self._source_path(source_id).unlink()
                raise
            return self._public_record(record)

    async def set_enabled(self, source_id: str, enabled: bool, *, deadline: float) -> bool:
        async with self._lock:
            records = self._load_records()
            record = self._find_record(records, source_id)
            sid = str(record["id"])
            if enabled:
                return await self._enable_record(records, record, deadline)
            return await self._disable_record(records, record, deadline)

    async def delete_source(self, source_id: str, *, deadline: float) -> bool:
        async with self._lock:
            records = self._load_records()
            record = self._find_record(records, source_id)
            sid = str(record["id"])
            if bool(record.get("enabled")):
                if not await self._disable_record(records, record, deadline):
                    return False
                records = self._load_records()
                record = self._find_record(records, source_id)
            runtime = self._runtimes.get(sid)
            if runtime is not None and not await self._stop_runtime(sid, runtime, self.stop_budget_s):
                return False
            records = [item for item in records if str(item.get("id") or "") != sid]
            self._save_records(records)
            order = self._read_order()
            if order:
                self._save_order([item for item in order if item != sid])
            with suppress(OSError):
                self._source_path(sid).unlink()
            self._last_errors.pop(sid, None)
            return True

    async def reorder_sources(self, source_ids: list[str]) -> dict:
        async with self._lock:
            records = self._load_records()
            by_id = {str(record.get("id") or ""): record for record in records}
            submitted = [str(item or "").strip() for item in (source_ids or []) if str(item or "").strip()]
            if len(set(submitted)) != len(submitted):
                raise SourceRuntimeError("duplicate_source_id", "source_order")
            unknown = [sid for sid in submitted if sid not in by_id]
            if unknown:
                raise SourceRuntimeError("unknown_source_id", "source_order")
            ordered = [by_id[sid] for sid in submitted]
            ordered.extend(record for record in records if str(record.get("id") or "") not in set(submitted))
            self._save_records(ordered)
            self._save_order([str(record.get("id") or "") for record in ordered])
            return {"success": True, "sourceIds": [str(record.get("id") or "") for record in ordered]}

    async def resolve(
        self,
        source_id: str,
        song_info: dict,
        quality: str,
        *,
        deadline: float,
    ) -> dict:
        sid = str(source_id or "")
        lock = self._source_locks.setdefault(sid, asyncio.Lock())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceResolveTimeout()
        try:
            async with asyncio.timeout(remaining):
                await lock.acquire()
        except TimeoutError as exc:
            raise SourceProcessBusy() from exc
        try:
            runtime = self._runtimes.get(sid)
            if runtime is None or runtime.exited:
                raise SourceProcessUnavailable("source_process_unavailable")
            record = self._find_record(self._load_records(), sid)
            if not bool(record.get("enabled")):
                raise SourceProcessUnavailable("source_not_enabled")
            if runtime.stopping:
                raise SourceProcessUnavailable("source_stopping")
            result = await runtime.resolve(
                platform=str(song_info.get("source") or ""),
                song_info=song_info,
                quality=str(quality or ""),
                deadline=deadline,
            )
            return {
                "url": str(result.get("url") or ""),
                "type": str(quality or ""),
                "sourceName": str(runtime.metadata.get("name") or record.get("name") or sid),
                "sourceId": sid,
                "attempts": [{"name": str(record.get("name") or sid), "status": "success"}],
                "hasMoreSources": False,
            }
        finally:
            lock.release()

    async def resolve_source_id(self, target: str) -> str:
        """Resolve exact IDs first; only accept a unique display name/URL fallback."""

        wanted = str(target or "").strip()
        records = self._load_records()
        exact = [record for record in records if str(record.get("id") or "") == wanted]
        if exact:
            return str(exact[0]["id"])
        matches = [
            record for record in records
            if str(record.get("name") or "") == wanted
            or str(record.get("sourceUrl") or "") == wanted
        ]
        if len(matches) == 1:
            return str(matches[0]["id"])
        raise SourceNotFound(wanted)

    async def _enable_record(self, records: list[dict], record: dict, deadline: float) -> bool:
        sid = str(record["id"])
        runtime = self._runtimes.get(sid)
        script = self._read_script(sid)
        script_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        previous = dict(record)
        if (
            bool(record.get("enabled"))
            and runtime is not None
            and not runtime.exited
            and not runtime.blocked
            and not runtime.cancel_unconfirmed
            and not runtime.stopping
            and runtime.script_hash == script_hash
        ):
            return True
        previous_runtime = runtime
        try:
            runtime = await self._start_record(record, deadline=deadline, script=script)
        except (SourceRuntimeError, OSError, ValueError) as exc:
            self._last_errors[sid] = self._error_code(exc)
            raise
        record["enabled"] = True
        self._merge_runtime_metadata(record, runtime)
        try:
            self._save_records(records)
        except BaseException:
            record.clear()
            record.update(previous)
            if runtime is not previous_runtime:
                await self._stop_runtime(sid, runtime, self.stop_budget_s)
            raise
        self._last_errors.pop(sid, None)
        return True

    async def _disable_record(self, records: list[dict], record: dict, deadline: float) -> bool:
        sid = str(record["id"])
        runtime = self._runtimes.get(sid)
        previous = dict(record)
        if runtime is not None:
            runtime.stopping = True
            runtime.blocked = True
            budget = min(self.stop_budget_s, max(0.05, deadline - time.monotonic()))
            stopped = await self._stop_runtime(sid, runtime, budget)
            if not stopped:
                return False
        record["enabled"] = False
        try:
            self._save_records(records)
        except BaseException:
            record.clear()
            record.update(previous)
            raise
        return True

    async def _start_record(
        self,
        record: dict,
        *,
        deadline: float,
        script: str | None = None,
    ) -> SourceProcess:
        sid = str(record.get("id") or "")
        _validate_source_id(sid)
        text = script if script is not None else self._read_script(sid)
        script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        current = self._runtimes.get(sid)
        if (
            current is not None
            and not current.exited
            and not current.stopping
            and not current.blocked
            and not current.cancel_unconfirmed
            and current.script_hash == script_hash
        ):
            return current
        if current is not None:
            if not await self._stop_runtime(sid, current, self.stop_budget_s):
                raise SourceProcessUnavailable("source_process_stop_failed")
        runtime_id = uuid.uuid4().hex
        runtime = await self._spawn_worker(
            source_id=sid,
            runtime_id=runtime_id,
            filename=sid,
            script=text,
            script_hash=script_hash,
            deadline=deadline,
        )
        self._runtimes[sid] = runtime
        self._merge_runtime_metadata(record, runtime)
        self._last_errors.pop(sid, None)
        if linux_monitoring_available():
            self.cpu.register(runtime.runtime_id, runtime.pid, runtime.start_time_ticks)
            try:
                self.cpu.sample(runtime.runtime_id, self.cpu.read_sample(runtime.runtime_id))
            except Exception:
                pass
        return runtime

    async def _validate_script(self, source_id: str, script: str, deadline: float) -> SourceProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceResolveTimeout()
        current = self._runtimes.get(source_id)
        if current is not None:
            if not await self._stop_runtime(source_id, current, min(self.stop_budget_s, remaining)):
                raise SourceProcessUnavailable("source_process_stop_failed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceResolveTimeout()
        runtime_id = uuid.uuid4().hex
        script_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        runtime = await self._spawn_worker(
            source_id=source_id,
            runtime_id=runtime_id,
            filename=source_id,
            script=script,
            script_hash=script_hash,
            deadline=deadline,
        )
        if not await self._stop_runtime(
            source_id, runtime, min(self.stop_budget_s, max(0.05, deadline - time.monotonic())),
        ):
            raise SourceProcessUnavailable("source_process_stop_failed")
        return runtime

    async def _spawn_worker(
        self,
        *,
        source_id: str,
        runtime_id: str,
        filename: str,
        script: str,
        script_hash: str,
        deadline: float,
    ) -> SourceProcess:
        self._check_node()
        if not self.lxserver_dir.is_dir():
            raise SourceInitializationFailed("lxserver_runtime_missing")
        if not self.worker_path.is_file():
            raise SourceInitializationFailed("source_worker_missing")
        parent_sock, child_sock = socket.socketpair()
        parent_sock.setblocking(False)
        child_sock.set_inheritable(True)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.node_bin,
                str(self.worker_path),
                "--fd",
                str(child_sock.fileno()),
                "--lxserver-dir",
                str(self.lxserver_dir),
                pass_fds=(child_sock.fileno(),),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=False,
            )
            child_sock.close()
            reader, writer = await asyncio.open_connection(
                sock=parent_sock,
                limit=MAX_INIT_FRAME_BYTES,
            )
        except BaseException:
            parent_sock.close()
            child_sock.close()
            if process is not None and process.returncode is None:
                with suppress(Exception):
                    process.kill()
                    await process.wait()
            raise
        start_time_ticks = 0
        if linux_monitoring_available():
            for _ in range(20):
                sample = read_proc_stat(process.pid)
                if sample is not None:
                    start_time_ticks = sample.start_time_ticks
                    break
                await asyncio.sleep(0.01)
            if start_time_ticks <= 0:
                with suppress(Exception):
                    process.kill()
                    await process.wait()
                writer.close()
                parent_sock.close()
                raise SourceInitializationFailed("process_identity_unavailable")
        runtime = SourceProcess(
            source_id=source_id,
            runtime_id=runtime_id,
            script_hash=script_hash,
            process=process,
            reader=reader,
            writer=writer,
            start_time_ticks=start_time_ticks,
            metadata={},
            platforms=[],
            qualities={},
        )
        self._runtimes[source_id] = runtime
        runtime.start_reader()
        request_id = uuid.uuid4().hex
        try:
            waiter = runtime.prepare_request(request_id)
            await runtime.send({
                "protocol": PROTOCOL_VERSION,
                "type": "init",
                "request_id": request_id,
                "runtime_id": runtime_id,
                "source_id": source_id,
                "filename": filename,
                "script": script,
            }, limit=MAX_INIT_FRAME_BYTES)
            message = await runtime.wait_request(
                request_id,
                min(deadline, time.monotonic() + self.init_timeout_s),
                future=waiter,
            )
            if message.get("type") != "ready" or message.get("ok") is not True:
                code = _safe_error_code(message.get("code"), "initialization_failed")
                if code == "unsafe_vm_required":
                    raise SourceUnsafeVmRequired()
                raise SourceInitializationFailed(code)
            runtime.metadata = dict(message.get("metadata") or {})
            runtime.platforms = [str(item) for item in (message.get("platforms") or [])]
            runtime.qualities = {
                str(key): [str(item) for item in value]
                for key, value in (message.get("qualities") or {}).items()
                if isinstance(value, list)
            }
            return runtime
        except BaseException:
            await self._stop_runtime(source_id, runtime, self.stop_budget_s)
            raise

    def _check_node(self) -> None:
        if self._node_checked:
            return
        node = shutil.which(self.node_bin)
        if not node:
            raise SourceInitializationFailed("node_not_found")
        try:
            output = subprocess.run(
                [node, "-p", "process.versions.node"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            major, minor = (int(part) for part in output.split(".")[:2])
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise SourceInitializationFailed("node_version_unknown") from exc
        if major < 22 or (major == 22 and minor < 13):
            raise SourceInitializationFailed("node_version_incompatible")
        self.node_bin = node
        self._node_checked = True

    async def _download_script(self, url: str, timeout: float) -> str:
        chunks = bytearray()
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async with client.stream(
                        "GET",
                        url,
                        headers={"User-Agent": "fnmusic-lx-source/1.0"},
                    ) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            if len(chunks) + len(chunk) > MAX_SCRIPT_BYTES:
                                raise SourceInitializationFailed("script_too_large")
                            chunks.extend(chunk)
        except TimeoutError as exc:
            raise SourceResolveTimeout() from exc
        except httpx.HTTPError as exc:
            raise SourceRuntimeError("source_download_failed", "source_import") from exc
        try:
            return chunks.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceInitializationFailed("script_not_utf8") from exc

    @staticmethod
    def _filename_from_url(url: str) -> str:
        try:
            from urllib.parse import unquote, urlsplit

            name = Path(unquote(urlsplit(url).path)).name
            return name or "custom_source.js"
        except (ValueError, UnicodeError):
            return "custom_source.js"

    def _new_record(
        self,
        source_id: str,
        script: str,
        script_hash: str,
        runtime: SourceProcess,
        *,
        source_url: str,
        filename: str,
    ) -> dict:
        meta = runtime.metadata
        return {
            "id": source_id,
            "name": str(meta.get("name") or parse_script_metadata(script, filename).name),
            "version": str(meta.get("version") or "1.0.0"),
            "author": str(meta.get("author") or ""),
            "description": str(meta.get("description") or ""),
            "homepage": str(meta.get("homepage") or ""),
            "size": len(script.encode("utf-8")),
            "supportedSources": list(runtime.platforms),
            "platforms": {
                platform: {"qualitys": list(runtime.qualities.get(platform, []))}
                for platform in runtime.platforms
            },
            "qualitys": {
                str(key): list(value) for key, value in (runtime.qualities or {}).items()
            },
            "enabled": False,
            "uploadTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sourceUrl": str(source_url or ""),
            "allowUnsafeVM": False,
            "requireUnsafe": False,
            "scriptSha256": script_hash,
        }

    def _load_records(self) -> list[dict]:
        raw = _read_json(self.sources_path, [])
        records: list[dict] = []
        seen: set[str] = set()
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("id") or "")
                if not sid or sid in seen or not SOURCE_ID_RE.fullmatch(sid):
                    continue
                record = dict(item)
                record["id"] = sid
                record["enabled"] = bool(record.get("enabled") or record.get("enable"))
                record.pop("enable", None)
                record.setdefault("name", sid.removesuffix(".js"))
                record.setdefault("version", "1.0.0")
                record.setdefault("supportedSources", [])
                records.append(record)
                seen.add(sid)
        if self.source_dir.is_dir():
            for path in sorted(self.source_dir.glob("*.js")):
                sid = path.name
                if sid in seen or not SOURCE_ID_RE.fullmatch(sid) or path.is_symlink():
                    continue
                try:
                    script = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                meta = parse_script_metadata(script, sid)
                records.append({
                    "id": sid,
                    "name": meta.name,
                    "version": meta.version,
                    "author": meta.author,
                    "description": meta.description,
                    "homepage": meta.homepage,
                    "size": len(script.encode("utf-8")),
                    "supportedSources": [],
                    "enabled": False,
                    "uploadTime": "",
                    "sourceUrl": "",
                    "allowUnsafeVM": False,
                    "requireUnsafe": False,
                })
                seen.add(sid)
        return records

    def _find_record(self, records: list[dict], target: str) -> dict:
        wanted = str(target or "").strip()
        for record in records:
            if str(record.get("id") or "") == wanted:
                return record
        matches = [
            record for record in records
            if str(record.get("name") or "") == wanted
            or str(record.get("sourceUrl") or "") == wanted
        ]
        if len(matches) == 1:
            return matches[0]
        raise SourceNotFound(wanted)

    def _save_records(self, records: list[dict]) -> None:
        sanitized = []
        for record in records:
            item = dict(record)
            item.pop("enable", None)
            item["enabled"] = bool(item.get("enabled"))
            sanitized.append(item)
        _atomic_write(
            self.sources_path,
            json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n",
        )

    def _read_order(self) -> list[str]:
        raw = _read_json(self.order_path, [])
        if not isinstance(raw, list):
            return []
        order: list[str] = []
        for item in raw:
            sid = str(item or "").strip()
            if sid and sid not in order:
                order.append(sid)
        return order

    def _save_order(self, source_ids: list[str]) -> None:
        _atomic_write(
            self.order_path,
            json.dumps([str(item) for item in source_ids if str(item)], ensure_ascii=False, indent=2) + "\n",
        )

    def _order_index(self, record: dict) -> tuple[int, str]:
        order = self._read_order()
        sid = str(record.get("id") or "")
        try:
            return (order.index(sid), sid)
        except ValueError:
            return (len(order), sid)

    def _source_path(self, source_id: str) -> Path:
        sid = _validate_source_id(source_id)
        root = self.source_dir.resolve()
        path = self.source_dir / sid
        if path.is_symlink():
            raise SourceRuntimeError("unsafe_source_file", "source_state")
        try:
            if path.resolve().parent != root:
                raise SourceRuntimeError("unsafe_source_file", "source_state")
        except OSError as exc:
            raise SourceRuntimeError("unsafe_source_file", "source_state") from exc
        return path

    def _read_script(self, source_id: str) -> str:
        path = self._source_path(source_id)
        try:
            if path.is_symlink() or path.stat().st_size > MAX_SCRIPT_BYTES:
                raise SourceInitializationFailed("script_too_large")
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SourceInitializationFailed("source_file_missing") from exc
        except (OSError, UnicodeError) as exc:
            raise SourceInitializationFailed("source_file_unreadable") from exc

    def _merge_runtime_metadata(self, record: dict, runtime: SourceProcess) -> None:
        meta = runtime.metadata
        for key in ("name", "version", "author", "description", "homepage"):
            value = meta.get(key)
            if value not in (None, ""):
                record[key] = str(value)
        record["supportedSources"] = list(runtime.platforms)
        record["platforms"] = {
            platform: {"qualitys": list(runtime.qualities.get(platform, []))}
            for platform in runtime.platforms
        }
        record["qualitys"] = {
            str(key): list(value) for key, value in (runtime.qualities or {}).items()
        }
        record["scriptSha256"] = runtime.script_hash
        record["allowUnsafeVM"] = False
        record["requireUnsafe"] = False

    def _public_record(self, record: dict) -> dict:
        sid = str(record.get("id") or "")
        runtime = self._runtimes.get(sid)
        enabled = bool(record.get("enabled"))
        status = "stopped"
        process_state = "disabled"
        error = ""
        runtime_id = ""
        raw_qualities = record.get("qualitys")
        qualities: dict[str, list[str]] = {
            str(key): [str(item) for item in value]
            for key, value in (raw_qualities or {}).items()
            if isinstance(value, list)
        } if isinstance(raw_qualities, dict) else {}
        raw_platforms = record.get("platforms")
        platforms: dict[str, dict] = {}
        if isinstance(raw_platforms, dict):
            for key, spec in raw_platforms.items():
                if not isinstance(spec, dict):
                    continue
                declared = spec.get("qualitys", spec.get("qualities", []))
                if isinstance(declared, list) and declared:
                    qualities.setdefault(str(key), [str(item) for item in declared])
                platforms[str(key)] = {"qualitys": list(qualities.get(str(key), []))}
        supported = record.get("supportedSources")
        if isinstance(supported, dict):
            supported = list(supported.keys())
        if isinstance(supported, list):
            for item in supported:
                key = str(item)
                if key:
                    platforms.setdefault(key, {"qualitys": list(qualities.get(key, []))})
        if runtime is not None:
            runtime_id = runtime.runtime_id
            process_state = "exited" if runtime.exited else "running"
            status = "success"
            if runtime.exited or runtime.process.returncode is not None:
                status = "failed"
                error = runtime.error or "source_process_exited"
            elif runtime.blocked or runtime.cancel_unconfirmed:
                status = "failed"
                error = runtime.error or "source_process_blocked"
            elif runtime.stopping:
                status = "loading"
                process_state = "stopping"
            qualities = {
                str(key): [str(item) for item in value]
                for key, value in (runtime.qualities or {}).items()
            }
            platforms = {
                key: {"qualitys": list(value)}
                for key, value in qualities.items()
            }
            if runtime.exited or runtime.process.returncode is not None:
                usage = self.cpu.mark_stopped(runtime.runtime_id)
            else:
                usage = self.cpu.snapshot(runtime.runtime_id)
        elif enabled:
            status = "failed"
            process_state = "unavailable"
            error = self._last_errors.get(sid, "source_process_unavailable")
            usage = self.cpu.snapshot("")
        else:
            usage = self.cpu.mark_stopped("")
        result = dict(record)
        result.update({
            "enable": enabled,
            "enabled": enabled,
            "status": status,
            "error": error,
            "platforms": platforms,
            "qualitys": qualities,
            "runtime_id": runtime_id,
            "process_state": process_state,
            "resource_usage": usage,
        })
        if not runtime and enabled:
            result["resource_usage"] = {
                **usage,
                "monitoring_status": "unknown",
                "cpu_percent": None,
            }
        return result

    async def _monitor_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.monitor_interval_s)
                async with self._lock:
                    runtimes = list(self._runtimes.values())
                for runtime in runtimes:
                    if runtime.exited or runtime.process.returncode is not None:
                        self.cpu.mark_stopped(runtime.runtime_id)
                        continue
                    sample = self.cpu.read_sample(runtime.runtime_id)
                    self.cpu.sample(runtime.runtime_id, sample)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("洛雪源 CPU 监控停止: %s", type(exc).__name__)

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        if isinstance(exc, SourceRuntimeError):
            return exc.code
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return "source_init_timeout"
        if isinstance(exc, FileNotFoundError):
            return "source_file_missing"
        if isinstance(exc, PermissionError):
            return "source_file_unreadable"
        return "initialization_failed"
