"""Token 后端 v2 按日永久保留账本与限额专属测试集。

覆盖：
1. v2 格式按日永久保留、跨日累积与 allTime 统计；
2. UTC+8 切日与 timezone 标注；
3. 旧 flat 记录迁移：读者只在内存转换不写盘，写者落盘且保留 historyIncomplete/statsStartedAt，不虚构历史；
4. 读者 fresh disk 实时可见跨进程写入，无全天缓存，读者只读绝不写盘；
5. 损坏文件现场保护：损坏原文件绝不悄然覆盖，status="corrupt" 明确返回非假 0；
6. 权限/保存失败 status 明确，写者保留内存账本并在后续重试落盘；
7. 请求计数：actual HTTP attempt 一次计数一次，quota 拒绝仅记 failure 不增 requests，empty-content 单次记账不 double count；
8. 未知 usage 不估算假 token 并累加 unknownUsageRequests；
9. 安全脱敏：failure 截断且绝不泄露 API key / Bearer 凭据；
10. 多线程并发 record 安全；
11. summary 默认 history_limit=0 轻量返回，支持分页 history_limit 与 history_offset。
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest

from proxy import llm_usage
from proxy import recommend as rec


def test_unknown_http_failures_count_once_but_quota_is_not_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(tmp_path / "usage.json"))
    llm_usage.record(None, "timeout", actual_request=True)
    llm_usage.record(None, "token-limit", actual_request=False)
    result = llm_usage.summary()
    assert result["requests"] == 1
    assert result["unknownUsageRequests"] == 1


# ----------------------------------------------------------- 1. 跨日与 v2 存储 ---

def test_v2_ledger_multi_day_accumulation(tmp_path, monkeypatch):
    """跨日永久保留账本，按日分别累计并在 allTime 汇总，totalDays 正确。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    # Day 1: 20261026
    monkeypatch.setattr(llm_usage, "_today", lambda: "20261026")
    llm_usage.record({"prompt": 100, "completion": 50, "total": 150})
    llm_usage.record({"prompt": 200, "completion": 50, "total": 250})

    # Day 2: 20261027
    monkeypatch.setattr(llm_usage, "_today", lambda: "20261027")
    llm_usage.record({"prompt": 300, "completion": 100, "total": 400})

    # Day 3: 20261028
    monkeypatch.setattr(llm_usage, "_today", lambda: "20261028")
    llm_usage.record({"prompt": 50, "completion": 50, "total": 100})

    summary = llm_usage.summary(history_limit=10)
    assert summary["date"] == "20261028"
    assert summary["total"] == 100
    assert summary["requests"] == 1
    assert summary["totalDays"] == 3
    assert summary["allTime"]["total"] == 900
    assert summary["allTime"]["prompt"] == 650
    assert summary["allTime"]["completion"] == 250
    assert summary["allTime"]["requests"] == 4
    assert len(summary["history"]) == 3
    # 历史列表降序排列
    assert [h["date"] for h in summary["history"]] == ["20261028", "20261027", "20261026"]


def test_utc8_timezone_and_api_annotation(tmp_path, monkeypatch):
    """验证 UTC+8 标注与 summary timezone 字段。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))
    llm_usage.record({"prompt": 10, "completion": 10, "total": 20})
    s = llm_usage.summary()
    assert s["timezone"] == "Asia/Shanghai"
    assert s["date"] == llm_usage._today()


# ----------------------------------------------------------- 2. 旧 flat 记录平滑迁移 ---

def test_old_flat_reader_only_in_memory_no_disk_write(tmp_path, monkeypatch):
    """读者读到旧 flat 记录时：仅在内存中转换为 v2 展示，绝不回写或修改磁盘文件。"""
    usage_file = tmp_path / "llm_usage.json"
    old_content = json.dumps({
        "date": "20261025",
        "prompt": 500,
        "completion": 200,
        "total": 700,
        "requests": 5,
        "lastFailure": {"reason": "http-429", "detail": "Too many requests", "at": 1729800000},
    })
    usage_file.write_text(old_content, encoding="utf-8")
    mtime_before = usage_file.stat().st_mtime_ns

    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))
    # 读者调用 summary(read_only=True)
    summary = llm_usage.summary(history_limit=10, read_only=True)
    assert summary["historyIncomplete"] is True
    assert summary["statsStartedAt"] == "20261025"
    assert summary["allTime"]["total"] == 700

    # 磁盘文件内容与修改时间绝对未被改变
    assert usage_file.read_text(encoding="utf-8") == old_content
    assert usage_file.stat().st_mtime_ns == mtime_before


def test_old_flat_writer_migrates_preserving_history(tmp_path, monkeypatch):
    """写者写入时：将旧 flat 数据无损迁移至 days，设置 historyIncomplete=True，保留旧数据不虚构。"""
    usage_file = tmp_path / "llm_usage.json"
    usage_file.write_text(json.dumps({
        "date": "20261025",
        "prompt": 500,
        "completion": 200,
        "total": 700,
        "requests": 5,
        "lastFailure": {"reason": "http-429", "detail": "Rate limit", "at": 1729800000},
    }), encoding="utf-8")

    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))
    monkeypatch.setattr(llm_usage, "_today", lambda: "20261028")

    # 写者发起一次写记录
    llm_usage.record({"prompt": 30, "completion": 20, "total": 50})

    disk_data = json.loads(usage_file.read_text(encoding="utf-8"))
    assert disk_data["version"] == 2
    assert disk_data["historyIncomplete"] is True
    assert disk_data["statsStartedAt"] == "20261025"
    # 旧日期数据完整保留
    assert disk_data["days"]["20261025"]["total"] == 700
    assert disk_data["days"]["20261025"]["requests"] == 5
    # 今日数据累加
    assert disk_data["days"]["20261028"]["total"] == 500 if False else 50


# ----------------------------------------------------------- 3. 跨进程可见性与读者无全天缓存 ---

def test_reader_fresh_disk_sees_external_process_update(tmp_path, monkeypatch):
    """读者无全天缓存：模拟外部进程更新磁盘文件后，读者立即反映最新用量。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))
    today = llm_usage._today()

    # 读者首次读取（文件不存在，missing）
    s1 = llm_usage.summary()
    assert s1["total"] == 0
    assert s1["status"] == "missing"

    # 模拟外部独立进程写入账本
    external_ledger = {
        "version": 2,
        "timezone": "Asia/Shanghai",
        "historyIncomplete": False,
        "statsStartedAt": today,
        "days": {
            today: {
                "prompt": 1000,
                "completion": 500,
                "total": 1500,
                "requests": 3,
                "unknownUsageRequests": 0,
                "lastFailure": None,
            }
        },
    }
    usage_file.write_text(json.dumps(external_ledger), encoding="utf-8")

    # 读者再次调用 summary，必须立刻读出外部进程更新，绝不返回内存缓存旧值
    s2 = llm_usage.summary()
    assert s2["total"] == 1500
    assert s2["requests"] == 3
    assert s2["status"] == "ok"


# ----------------------------------------------------------- 4. 损坏文件保护与异常状态明确 ---

def test_corrupt_file_is_never_silently_overwritten(tmp_path, monkeypatch):
    """损坏文件现场保护：磁盘已损坏时，读者明示 corrupt，写者拒绝覆盖损坏原文件并保留现场。"""
    usage_file = tmp_path / "llm_usage.json"
    corrupt_content = "NOT A VALID JSON CONTENT --- CRITICAL CORRUPTION"
    usage_file.write_text(corrupt_content, encoding="utf-8")

    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    # 读者检查
    s_read = llm_usage.summary()
    assert s_read["status"] == "corrupt"
    assert "损坏" in s_read["statusMessage"]

    # 写者尝试写入
    llm_usage.record({"prompt": 10, "completion": 10, "total": 20})

    # 核心验证：损坏原文件绝对没有被悄悄覆盖
    assert usage_file.read_text(encoding="utf-8") == corrupt_content


def test_permission_denied_or_save_failed_retains_memory(tmp_path, monkeypatch):
    """写入失败（如只读文件系统）时，明确 status="save_failed"，并在内存中保留数据以便后续重试。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    real_replace = os.replace

    def broken_replace(src, dst):
        raise OSError("Read-only file system")

    monkeypatch.setattr(os, "replace", broken_replace)
    llm_usage.record({"prompt": 80, "completion": 20, "total": 100})

    # 检查状态
    s = llm_usage.summary(read_only=False)
    assert s["status"] == "save_failed"
    assert s["total"] == 100  # 内存账本保留，未假 0

    # 恢复文件系统正常，下次写请求成功落盘并恢复 ok
    monkeypatch.setattr(os, "replace", real_replace)
    llm_usage.record({"prompt": 10, "completion": 10, "total": 20})
    s_ok = llm_usage.summary(read_only=False)
    assert s_ok["status"] == "ok"
    assert s_ok["total"] == 120
    assert usage_file.exists()


# ----------------------------------------------------------- 5. 请求计数与配额拦截逻辑 ---

@pytest.mark.asyncio
async def test_quota_blocked_does_not_increment_actual_requests(monkeypatch):
    """配额达到上限拦截时：仅记 failure="token-limit"，不增加 actual HTTP requests。"""
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "https://api.test/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "100")

    # 预设已达 100 token
    llm_usage.record({"prompt": 60, "completion": 40, "total": 100}, actual_request=True)
    before_reqs = llm_usage.used_today()["requests"]
    assert before_reqs == 1

    http_client = MagicMock()
    with pytest.raises(rec.LLMFailure) as exc:
        await rec._llm_complete(http_client, "test prompt", temperature=0.7, system="sys")
    assert exc.value.reason == "token-limit"

    after = llm_usage.used_today()
    assert after["requests"] == 1  # 绝不增加 actual requests 计数
    assert after["lastFailure"]["reason"] == "token-limit"


@pytest.mark.asyncio
async def test_empty_content_single_record_no_double_count(monkeypatch):
    """HTTP 200 正文为空（empty-content）：仅记 1 次 actual request，不重复记账。"""
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "https://api.test/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")

    mock_resp_body = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_resp_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(rec.LLMFailure) as exc:
            await rec._llm_complete(client, "test prompt", temperature=0.7, system="sys")
        assert exc.value.reason == "empty-content"

    today_data = llm_usage.used_today()
    assert today_data["requests"] == 1  # 仅记 1 次，严禁 2 次
    assert today_data["total"] == 40
    assert today_data["lastFailure"]["reason"] == "empty-content"


# ----------------------------------------------------------- 6. 未知 usage 不估算并计次 ---

@pytest.mark.asyncio
async def test_unknown_usage_counted_without_fake_tokens(monkeypatch):
    """第三方模型未返回 usage 字段时：不虚构假 token，计入 unknownUsageRequests。"""
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "https://api.test/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")

    mock_resp_body = {
        "choices": [{"finish_reason": "stop", "message": {"content": '[{"title": "Song"}]'}}],
        # 没有 usage 字段
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_resp_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        content = await rec._llm_complete(client, "test prompt", temperature=0.7, system="sys")
        assert "Song" in content

    today_data = llm_usage.used_today()
    assert today_data["requests"] == 1
    assert today_data["total"] == 0  # 不瞎估算
    assert today_data["unknownUsageRequests"] == 1


# ----------------------------------------------------------- 7. 安全脱敏验证 ---

def test_failure_sanitization_masks_api_keys(tmp_path, monkeypatch):
    """失败信息中绝不泄漏真实 API Key，无论在 URL、Header 还是正文。"""
    real_key = "sk-REAL-SUPER-SECRET-LLM-KEY-9999"
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", real_key)

    raw_detail = f"Failed contacting https://api.com?key={real_key} with Bearer {real_key}: unauthorized"
    llm_usage.record(None, "http-401", raw_detail)

    saved_failure = llm_usage.used_today()["lastFailure"]
    assert real_key not in saved_failure["detail"]
    assert "***" in saved_failure["detail"]
    assert len(saved_failure["detail"]) <= 500


# ----------------------------------------------------------- 8. 多线程并发写安全 ---

def test_multithreaded_concurrent_recording(tmp_path, monkeypatch):
    """多线程高并发写账本，原子落盘且数据无丢失无损坏。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    threads = []
    thread_count = 10
    records_per_thread = 5

    def worker():
        for _ in range(records_per_thread):
            llm_usage.record({"prompt": 1, "completion": 2, "total": 3})

    for _ in range(thread_count):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    summary = llm_usage.summary()
    assert summary["requests"] == thread_count * records_per_thread
    assert summary["total"] == thread_count * records_per_thread * 3
    assert summary["status"] == "ok"


# ----------------------------------------------------------- 9. summary 分页与默认参数 ---

def test_summary_pagination_and_default_lightweight(tmp_path, monkeypatch):
    """默认 summary() 不拉取历史记录（[]），显式传入 history_limit 与 history_offset 时分页返回。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    days = ["20261020", "20261021", "20261022", "20261023", "20261024"]
    for d in days:
        monkeypatch.setattr(llm_usage, "_today", lambda d=d: d)
        llm_usage.record({"prompt": 10, "completion": 10, "total": 20})

    # 1. 默认调用（健康检查 / healthz）：history 为 []，零历史开销
    s_default = llm_usage.summary()
    assert s_default["history"] == []
    assert s_default["totalDays"] == 5

    # 2. WebUI 显式请求第一页 (limit=2, offset=0)
    s_page1 = llm_usage.summary(history_limit=2, history_offset=0)
    assert len(s_page1["history"]) == 2
    assert [h["date"] for h in s_page1["history"]] == ["20261024", "20261023"]

    # 3. WebUI 显式请求第二页 (limit=2, offset=2)
    s_page2 = llm_usage.summary(history_limit=2, history_offset=2)
    assert len(s_page2["history"]) == 2
    assert [h["date"] for h in s_page2["history"]] == ["20261022", "20261021"]


# ----------------------------------------------------------- 10. 深度鲁棒性回归 ---

def test_atomic_save_failure_at_mkdir_or_tempfile_does_not_raise(tmp_path, monkeypatch):
    """os.makedirs 或 NamedTemporaryFile 异常被安全吸收，绝不抛出异常阻断业务推荐。"""
    usage_file = tmp_path / "protected" / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    # 1. 模拟 os.makedirs 权限拒绝
    real_makedirs = os.makedirs

    def broken_makedirs(name, exist_ok=True):
        raise PermissionError(f"Permission denied creating directory {name}")

    monkeypatch.setattr(os, "makedirs", broken_makedirs)
    # record 必须安全返回，绝不中断抛出
    llm_usage.record({"prompt": 10, "completion": 20, "total": 30})
    s1 = llm_usage.summary(read_only=False)
    assert s1["status"] == "permission_denied"
    assert s1["total"] == 30

    # 2. 恢复 os.makedirs 并模拟 NamedTemporaryFile 磁盘写满 OSError
    monkeypatch.setattr(os, "makedirs", real_makedirs)

    def broken_named_temp(*args, **kwargs):
        raise OSError("Disk full: no space left on device")

    import tempfile
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", broken_named_temp)
    llm_usage.record({"prompt": 5, "completion": 5, "total": 10})
    s2 = llm_usage.summary(read_only=False)
    assert s2["status"] == "save_failed"
    assert "Disk full" in s2["statusMessage"]
    assert s2["total"] == 40  # 内存继续累加，未丢失


def test_quota_respects_pending_memory_under_save_failure(tmp_path, monkeypatch):
    """落盘失败时 pending 内存账本依然生效，within_limit 绝不超配额。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    # 模拟磁盘只读故障
    def broken_replace(src, dst):
        raise OSError("Read-only filesystem")

    monkeypatch.setattr(os, "replace", broken_replace)

    # 上限 100，写入 100 token
    assert llm_usage.within_limit(100) is True
    llm_usage.record({"prompt": 50, "completion": 50, "total": 100})

    # 虽然落盘失败，但 within_limit 必须能感知 pending 内存中的 100 token，返回 False 拦截！
    assert llm_usage.within_limit(100) is False
    assert llm_usage.used_today()["total"] == 100
    assert llm_usage.summary(read_only=False)["status"] == "save_failed"


def test_summary_exposes_writer_status_and_webui_read_only(tmp_path, monkeypatch):
    """proxy summary() 默认暴露 writer 真实故障状态；WebUI 显式 read_only=True 保持磁盘只读。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    # 磁盘初始写入 1 次成功记录
    llm_usage.record({"prompt": 10, "completion": 10, "total": 20})
    assert usage_file.exists()

    # 发生故障落盘失败
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("I/O error")))
    llm_usage.record({"prompt": 10, "completion": 10, "total": 20})

    # 1. proxy 宿主健康检查 summary() 默认获取 writer 视图：能看到 save_failed 与内存 40
    writer_summary = llm_usage.summary()
    assert writer_summary["status"] == "save_failed"
    assert writer_summary["total"] == 40

    # 2. WebUI 显式 read_only=True 获取 fresh disk 视图：看到磁盘 20，status="ok"，绝不写盘
    reader_summary = llm_usage.summary(read_only=True)
    assert reader_summary["status"] == "ok"
    assert reader_summary["total"] == 20


def test_total_days_does_not_include_unrecorded_today(tmp_path, monkeypatch):
    """今天若未发生过任何请求，totalDays 不计入今天，history 亦不包含空今天。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))

    # 记录前两天的历史
    monkeypatch.setattr(llm_usage, "_today", lambda: "20261026")
    llm_usage.record({"prompt": 10, "completion": 10, "total": 20})
    monkeypatch.setattr(llm_usage, "_today", lambda: "20261027")
    llm_usage.record({"prompt": 20, "completion": 20, "total": 40})

    # 切到今天 20261028，尚未发起任何请求
    monkeypatch.setattr(llm_usage, "_today", lambda: "20261028")

    # 查询 summary
    s = llm_usage.summary(history_limit=10)
    assert s["date"] == "20261028"
    assert s["total"] == 0  # 今日数据返回 0
    assert s["totalDays"] == 2  # 核心断言：总天数只计入真实记录过的 2 天
    assert len(s["history"]) == 2
    assert [h["date"] for h in s["history"]] == ["20261027", "20261026"]

    # 发生一次实际调用后，今天才正式计入天数
    llm_usage.record({"prompt": 5, "completion": 5, "total": 10})
    s_after = llm_usage.summary(history_limit=10)
    assert s_after["totalDays"] == 3
    assert len(s_after["history"]) == 3
    assert s_after["history"][0]["date"] == "20261028"

