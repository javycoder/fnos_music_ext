"""聚合截止信息不能被当作完整搜索成功而丢弃。"""
import httpx
import pytest

from proxy import app as proxy


@pytest.mark.anyio
async def test_only_legacy_early_return_diagnostics_are_filtered():
    errors = {
        "slow": "skipped (deadline)",
        "partial": "skipped (deadline) (partial results)",
        "fast": "skipped (fast return)",
        "grace": "skipped (grace elapsed)",
        "broken": "timeout after 20s",
    }
    async with httpx.AsyncClient(
        base_url="http://musicdl.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"ok": True, "items": [], "errors": errors})),
    ) as client:
        result = await proxy.fetch_musicdl_search(client, "test", 5)
    assert result["errors"] == {
        key: value for key, value in errors.items() if key in ("slow", "partial", "broken")}
