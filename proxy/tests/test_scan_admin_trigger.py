"""管理员扫描分支端到端触发测试（复现 authx_header 未导入的 NameError）。

背景（3.2.0 实机）：配置管理员凭据 + 官方扫描路径（_ADMIN_SCAN_PATH）时，
扫库请求从未发出：worker 与手动扫库都调用 app.py 级的 authx_header 构造
签名头，但该符号定义在 scan_auth.py 而 app.py 只导入了 ScanAdminSession /
ScanAuthError——NameError 被 except Exception 吞成 outcome="exception"。
现有用例只覆盖自定义路径分支（不带 authx_header），该断点无测试触达。
"""
import httpx
import pytest

from proxy import app as proxy_app

_SCAN_BODY = '{"guid":"1d5041882c2d420598f7cf1488d341b8"}'


class _StubAdminSession:
    """跳过真实登录：仅提供会话头与失效钩子。"""

    def __init__(self):
        self.invalidated = 0

    async def headers(self, conf):
        return {"cookie": "music-token=fixture-session"}

    def invalidate(self):
        self.invalidated += 1


@pytest.mark.asyncio
async def test_scan_worker_admin_branch_sends_request(monkeypatch, caplog):
    """管理员凭据 + 官方扫描路径：落盘后的扫描必须真正发出请求并带 authx 头。"""
    monkeypatch.setitem(proxy_app.CONF, "library_scan_path",
                        proxy_app._ADMIN_SCAN_PATH + "|" + _SCAN_BODY)
    monkeypatch.setitem(proxy_app.CONF, "scan_admin_username", "admin")
    monkeypatch.setitem(proxy_app.CONF, "scan_admin_password", "secret")
    monkeypatch.setattr(proxy_app, "_scan_pending_headers", {"Cookie": "ignored"})
    monkeypatch.setattr(proxy_app, "_scan_last_attempt", 0)
    monkeypatch.setattr(proxy_app, "_scan_admin_session", _StubAdminSession())

    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://official.test") as client:
        monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: client)
        await proxy_app._run_scan_worker(0)

    assert captured, "扫描请求未发出：管理员分支 authx_header NameError 被吞（outcome=exception）"
    assert captured[0].url.path == proxy_app._ADMIN_SCAN_PATH
    assert captured[0].headers.get("authx"), "扫描请求必须带 authx 签名头"
    assert captured[0].headers.get("cookie") == "music-token=fixture-session"
    assert _SCAN_BODY.encode() in captured[0].read()


@pytest.mark.asyncio
async def test_maintenance_scan_user_admin_path_sends_request(monkeypatch):
    """UI 手动扫库（scan-user）走管理员路径时必须发出扫描请求。"""
    monkeypatch.setitem(proxy_app.CONF, "library_scan_path",
                        proxy_app._ADMIN_SCAN_PATH + "|" + _SCAN_BODY)
    monkeypatch.setitem(proxy_app.CONF, "scan_admin_username", "admin")
    monkeypatch.setitem(proxy_app.CONF, "scan_admin_password", "secret")
    monkeypatch.setattr(proxy_app, "_scan_admin_session", _StubAdminSession())
    monkeypatch.setattr(proxy_app, "list_music_users",
                        lambda: [{"guid": "u1", "name": "甲", "role": "admin", "status": "active"}])

    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://official.test") as client:
        monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: client)
        status, payload = await proxy_app._maintenance_scan_user("u1")

    assert status == 200
    assert payload.get("ok") is True, f"手动扫描未确认：{payload}"
    assert captured, "手动扫描请求未发出（authx_header NameError 被吞）"
    assert captured[0].headers.get("authx"), "手动扫描必须带 authx 签名头"


def test_scan_slot_records_wall_clock_for_ui(monkeypatch, tmp_path):
    """扫描状态须记录墙钟时间并由 user-storage 状态带回（UI「最近扫描」展示）。"""
    import time as _time
    guid = "u9-wall-clock"
    before = _time.time()
    try:
        proxy_app._set_scan_slot(guid, last_attempt=_time.monotonic(), status="ok")
        state = proxy_app._scan_state(guid)
        assert before - 1 <= state.get("last_at", 0) <= _time.time() + 1
        assert state["status"] == "ok"

        monkeypatch.setitem(proxy_app.CONF, "tee_save_dir", str(tmp_path))
        monkeypatch.setitem(proxy_app.CONF, "tee_save_dir_by_user", "")
        monkeypatch.setitem(proxy_app.CONF, "library_scan_by_user", "")
        monkeypatch.setitem(proxy_app.CONF, "library_scan_path", "")
        status = proxy_app._user_storage_status(
            [{"guid": guid, "name": "甲", "role": "admin", "status": "active"}])
        entry = next(e for e in status["users"] if e["guid"] == guid)
        assert entry["scan_status"] == "ok"
        assert entry["scan_last_at"] == state["last_at"], "user-storage 必须带回扫描墙钟时间"
    finally:
        proxy_app._scan_state_by_user.pop(guid, None)
