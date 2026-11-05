import os
import pytest
from config_jobs import ConfigJobStore
from test_webui import env_file, svctl, lx_ready_mock


def test_config_job_store_crud(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    store = ConfigJobStore(db_file)

    # 初始状态无活动任务
    assert store.get_active_job() is None

    # 创建任务
    job = store.create_job(
        payload={"FNMUSIC_LX_ENABLED": "true"},
        base_revision="rev_123",
        idempotency_key="idemp_abc",
    )
    assert job.status == "pending"
    assert job.progress == 0
    assert job.idempotency_key == "idemp_abc"

    # 查询活动任务
    active = store.get_active_job()
    assert active is not None
    assert active.job_id == job.job_id

    # 幂等键查找
    found = store.find_by_idempotency_key("idemp_abc")
    assert found is not None
    assert found.job_id == job.job_id

    # 更新阶段与进度
    updated = store.update_job(
        job.job_id,
        status="running",
        phase="gate_checking",
        progress=30,
        message="快速门禁中",
    )
    assert updated.status == "running"
    assert updated.phase == "gate_checking"
    assert updated.progress == 30
    assert updated.message == "快速门禁中"

    # 完成任务
    completed = store.update_job(
        job.job_id,
        status="success",
        phase="done",
        progress=100,
        new_revision="rev_456",
        result={"changed": ["FNMUSIC_LX_ENABLED"]},
    )
    assert completed.status == "success"
    assert completed.progress == 100
    assert completed.result == {"changed": ["FNMUSIC_LX_ENABLED"]}

    # 完成后无活动任务
    assert store.get_active_job() is None


def test_api_config_jobs_endpoints(env_file, svctl, tmp_path, monkeypatch):
    import time
    import test_webui
    from app import app, job_store, compute_config_revision

    db_path = tmp_path / "test_api_jobs.db"
    monkeypatch.setattr(job_store, "db_path", db_path)
    job_store._init_db()

    idem_key = f"idem_test_{time.time()}"

    with test_webui.authed_client() as client:
        # 1. 提交配置保存任务
        res = client.post("/api/config/jobs", json={
            "values": {"FNMUSIC_QUALITY_MODE": "balanced"},
            "idempotency_key": idem_key,
        })
        assert res.status_code == 202, f"Expected 202 but got {res.status_code}: {res.text}"
        data = res.json()
        assert data["ok"] is True
        job_id = data["job_id"]

        # 等待后台任务执行完成
        for _ in range(50):
            res_get = client.get(f"/api/config/jobs/{job_id}")
            assert res_get.status_code == 200
            job_data = res_get.json()["job"]
            if job_data["status"] in ("success", "partial", "failed"):
                break
            time.sleep(0.05)

        assert job_data["job_id"] == job_id
        assert job_data["status"] == "success"
        assert job_data["progress"] == 100

        # 2. 幂等去重提交：返回 200 并携带 deduplicated=True
        res_dup = client.post("/api/config/jobs", json={
            "values": {"FNMUSIC_QUALITY_MODE": "balanced"},
            "idempotency_key": idem_key,
        })
        assert res_dup.status_code == 200
        assert res_dup.json()["deduplicated"] is True
        assert res_dup.json()["job"]["job_id"] == job_id

        # 3. GET /api/config 携带 revision
        res_cfg = client.get("/api/config")
        assert res_cfg.status_code == 200
        assert "revision" in res_cfg.json()
        rev = res_cfg.json()["revision"]
        assert isinstance(rev, str) and len(rev) == 16

