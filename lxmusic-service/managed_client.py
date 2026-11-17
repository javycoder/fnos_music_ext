"""HTTP-compatible LX client backed by the per-source runtime manager."""

from __future__ import annotations

import time
from typing import Any

from lxserver_client import LxServerClient, UnsafeVmRequired
from source_runtime import (
    SourceAlreadyExists,
    SourceInitializationFailed,
    SourceNotFound,
    SourceProcessBusy,
    SourceProcessUnavailable,
    SourceResolveTimeout,
    SourceRuntimeError,
    SourceRuntimeManager,
    SourceScriptFailed,
    SourceUnsafeVmRequired,
)


class ManagedLxServerClient(LxServerClient):
    """Keep search/metadata on lxserver; route source lifecycle and resolve locally.

    ``manager`` is owned by the application lifespan. This client does not close
    it so callers can stop workers before closing the shared HTTP transport.
    """

    def __init__(
        self,
        *args,
        manager: SourceRuntimeManager | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.manager = manager

    def _require_manager(self) -> SourceRuntimeManager:
        if self.manager is None:
            raise SourceRuntimeError("source_runtime_unavailable", "source_runtime")
        return self.manager

    def _management_deadline(self) -> float:
        return time.monotonic() + max(0.01, float(self.management_timeout))

    @staticmethod
    def _safe_message(exc: BaseException) -> str:
        if isinstance(exc, SourceRuntimeError):
            return f"{exc.stage}: {exc.code}"
        name = type(exc).__name__
        return f"source runtime error: {name}"

    @staticmethod
    def _source_payload(record: dict[str, Any]) -> dict[str, Any]:
        meta = {
            "name": str(record.get("name") or ""),
            "version": str(record.get("version") or "1.0.0"),
            "author": str(record.get("author") or ""),
            "description": str(record.get("description") or ""),
            "homepage": str(record.get("homepage") or ""),
        }
        return {
            "success": True,
            "id": str(record.get("id") or ""),
            "metadata": meta,
            "supportedSources": list(record.get("supportedSources") or []),
            "owner": "open",
            "allowUnsafeVM": False,
            "requireUnsafe": False,
        }

    @staticmethod
    def _translate_initialization_error(exc: BaseException) -> RuntimeError:
        if isinstance(exc, SourceUnsafeVmRequired):
            return UnsafeVmRequired(
                "脚本需要 lxserver 的不安全原生 VM 运行环境；本项目不自动授权、本次未做验证"
            )
        if isinstance(exc, SourceAlreadyExists):
            return RuntimeError("源已存在")
        if isinstance(exc, SourceNotFound):
            return RuntimeError("源不存在")
        if isinstance(exc, SourceInitializationFailed):
            return RuntimeError(f"源初始化失败: {exc.code}")
        if isinstance(exc, SourceResolveTimeout):
            return TimeoutError("source initialization timed out")
        if isinstance(exc, SourceRuntimeError):
            return RuntimeError(f"源运行时错误: {exc.stage}: {exc.code}")
        if isinstance(exc, (TimeoutError,)):
            return TimeoutError("source initialization timed out")
        return RuntimeError(f"源运行时错误: {type(exc).__name__}")

    async def list_custom_sources(self) -> list[dict]:
        if self.manager is None:
            return await super().list_custom_sources()
        return await self.manager.list_sources()

    async def import_custom_source(self, url: str, *, allow_unsafe_vm: bool = False) -> dict:
        if allow_unsafe_vm:
            raise UnsafeVmRequired("本项目不开启 lxserver 不安全 VM；本次未做验证")
        manager = self._require_manager()
        try:
            record = await manager.import_source(url, deadline=self._management_deadline())
        except Exception as exc:
            if isinstance(exc, (UnsafeVmRequired, RuntimeError)) and not isinstance(exc, SourceRuntimeError):
                raise
            raise self._translate_initialization_error(exc) from None
        return self._source_payload(record)

    async def upload_custom_source(
        self,
        filename: str,
        script_content: str,
        *,
        allow_unsafe_vm: bool = False,
    ) -> dict:
        if allow_unsafe_vm:
            raise UnsafeVmRequired("本项目不开启 lxserver 不安全 VM；本次未做验证")
        manager = self._require_manager()
        try:
            record = await manager.upload_source(
                filename,
                script_content,
                deadline=self._management_deadline(),
            )
        except Exception as exc:
            if isinstance(exc, (UnsafeVmRequired, RuntimeError)) and not isinstance(exc, SourceRuntimeError):
                raise
            raise self._translate_initialization_error(exc) from None
        return self._source_payload(record)

    async def toggle_custom_source(self, source_id: str, enable: bool) -> bool:
        manager = self._require_manager()
        try:
            return await manager.set_enabled(
                source_id, bool(enable), deadline=self._management_deadline()
            )
        except SourceNotFound:
            return False
        except SourceRuntimeError as exc:
            raise RuntimeError(f"源切换失败: {exc.code}") from None

    async def set_source_enabled(self, target_id_or_name: str, enabled: bool) -> bool:
        manager = self._require_manager()
        try:
            source_id = await manager.resolve_source_id(target_id_or_name)
        except SourceNotFound:
            return False
        return await self.toggle_custom_source(source_id, enabled)

    async def delete_custom_source(self, source_id: str) -> bool:
        manager = self._require_manager()
        try:
            return await manager.delete_source(
                source_id, deadline=self._management_deadline()
            )
        except SourceNotFound:
            return False
        except SourceRuntimeError as exc:
            raise RuntimeError(f"源删除失败: {exc.code}") from None

    async def reorder_custom_sources(self, source_ids: list[str]) -> dict:
        manager = self._require_manager()
        try:
            return await manager.reorder_sources(source_ids)
        except SourceRuntimeError as exc:
            raise RuntimeError(f"源排序失败: {exc.code}") from None

    async def get_music_url(
        self,
        song_info: dict,
        quality: str = "128k",
        exclude_api_sources: list[str] | None = None,
        request_timeout: float | None = None,
        auto_switch_api_source: bool = True,
        *,
        source_id: str | None = None,
    ) -> dict | None:
        if not source_id:
            return await super().get_music_url(
                song_info,
                quality,
                exclude_api_sources=exclude_api_sources,
                request_timeout=request_timeout,
                auto_switch_api_source=auto_switch_api_source,
            )
        manager = self._require_manager()
        budget = float(request_timeout) if request_timeout is not None else float(self.timeout)
        deadline = time.monotonic() + max(0.01, budget)
        try:
            return await manager.resolve(source_id, song_info, quality, deadline=deadline)
        except SourceResolveTimeout as exc:
            raise TimeoutError("source resolve timeout") from exc
        except SourceProcessBusy as exc:
            raise TimeoutError("source process busy") from exc
        except SourceProcessUnavailable as exc:
            raise RuntimeError(f"source process unavailable: {exc.code}") from None
        except SourceScriptFailed as exc:
            raise RuntimeError(f"source script failed: {exc.code}") from None
