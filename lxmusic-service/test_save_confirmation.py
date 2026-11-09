"""快速保存的服务端时限、只读回查和启动恢复隔离。"""
import asyncio
import json
import time

from conftest import lxapp
from lxserver_client import management_operation


def test_source_confirmation_cancels_inside_management_lock(monkeypatch, fake_lx):
    cancelled = []

    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(lxapp, "_find_lxserver_source", slow)

    async def run():
        started = time.monotonic()
        response = await lxapp.source_set(lxapp.SourceBody(url="https://s/a.js", timeout_s=0.03))
        assert time.monotonic() - started < 0.3
        assert response.status_code == 504
        assert json.loads(response.body)["category"] == "result_unknown"
        assert cancelled
        async with management_operation():
            pass

    asyncio.run(run())


def test_readonly_snapshot_does_not_sync_order_or_persist_cleanup(monkeypatch):
    async def query():
        return []

    def policy(sources, **kwargs):
        assert kwargs.get("persist_cleanup", False) is False
        return {}

    async def forbidden(*args):
        raise AssertionError("只读回查不能写入上游排序")

    monkeypatch.setattr(lxapp, "_query_source_list", query)
    monkeypatch.setattr(lxapp, "_policy_payload", policy)
    monkeypatch.setattr(lxapp, "_sync_upstream_order", forbidden)
    assert asyncio.run(lxapp.source_info(read_only=True))["ok"] is True


def test_bootstrap_cannot_replay_old_targets_after_manual_save(monkeypatch):
    monkeypatch.setattr(lxapp, "_SOURCE_MANUAL_REVISION", 0)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        restored = []

        async def alive():
            entered.set()
            await release.wait()
            return True

        async def restore():
            restored.append(True)

        monkeypatch.setattr(lxapp.LXSERVER, "is_alive", alive)
        monkeypatch.setattr(lxapp, "_migrate_persisted_sources", restore)
        task = asyncio.create_task(lxapp._bootstrap_migration())
        await entered.wait()
        lxapp._SOURCE_MANUAL_REVISION += 1
        release.set()
        await task
        assert restored == []

    asyncio.run(run())


def test_late_bootstrap_skips_recovery_when_manual_save_already_started(monkeypatch):
    monkeypatch.setattr(lxapp, "_SOURCE_MANUAL_REVISION", 1)

    async def forbidden():
        raise AssertionError("保存接管后不应重新启动旧恢复任务")

    monkeypatch.setattr(lxapp.LXSERVER, "is_alive", forbidden)
    asyncio.run(lxapp._bootstrap_migration())


def test_save_reorder_can_confirm_without_writing_config(monkeypatch, fake_lx):
    def forbidden(*args):
        raise AssertionError("配置保存只允许 WebUI 统一提交排序")

    monkeypatch.setattr(lxapp, "_write_source_order_file", forbidden)
    result = asyncio.run(lxapp.source_reorder(lxapp.ReorderBody(
        source_ids=["source1"], persist=False, timeout_s=0.5)))
    assert result["ok"] is True


def test_bootstrap_syncs_persisted_order_without_relying_on_read_queries(monkeypatch, fake_lx, tmp_path):
    items = [{"url": sid, "active": True} for sid in ("source1", "source2")]
    path = tmp_path / ".env"
    path.write_text(f"LX_SOURCE_LIST='{json.dumps(items)}'\n"
                    "FNMUSIC_LX_SOURCE_ORDER='[\"source2\",\"source1\"]'\n")
    monkeypatch.setenv("LX_ENV_PATH", str(path))
    monkeypatch.setattr(lxapp, "_SOURCE_MANUAL_REVISION", 0)
    monkeypatch.setattr(lxapp, "_LAST_UPSTREAM_ORDER", None)
    for source in fake_lx.sources:
        source.update(enabled=True, status="success")
    asyncio.run(lxapp._bootstrap_migration())
    assert [s["id"] for s in fake_lx.sources] == ["source2", "source1"]
