from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from proxy.self_update import (
    FALLBACK_FAIL_MESSAGE,
    TargetSnapshot,
    UpdateJob,
    _download_and_verify,
    create_data_backup,
    get_capability,
    get_target_snapshot,
    resolve_target,
    start_update_job,
)


def test_get_capability():
    cap = get_capability()
    assert cap["allowed"] is True
    assert cap["target_arch"] in ("amd64", "arm64")


def test_resolve_target_priority_gitee_then_github():
    gh_release = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "source": "github",
        "assets": [
            {
                "name": "fnmusic-ext-2.12.4-0.fpk",
                "browser_download_url": "https://github.com/dl/fnmusic-ext-2.12.4-0.fpk",
                "size": 5000,
                "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }
        ],
    }
    gt_release = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "source": "gitee",
        "assets": [
            {
                "name": "fnmusic-ext-2.12.4-0.fpk",
                "browser_download_url": "https://gitee.com/dl/fnmusic-ext-2.12.4-0.fpk",
                "size": 5000,
            }
        ],
    }

    def mock_fetch(url: str):
        if "gitee.com" in url:
            return gt_release
        if "github.com" in url:
            return gh_release
        return None

    snap = resolve_target("v2.12.4", arch="amd64", http_getter=mock_fetch)
    assert snap is not None
    assert snap.install_version == "2.12.4-0"
    assert snap.source == "gitee"  # Gitee takes priority
    assert len(snap.candidates) == 2
    assert snap.candidates[0]["source"] == "gitee"
    assert snap.candidates[1]["source"] == "github"
    assert snap.sha256 == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_resolve_target_fallback_github_when_gitee_has_no_fpk():
    gh_release = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "source": "github",
        "assets": [
            {
                "name": "fnmusic-ext-2.12.4-0.fpk",
                "browser_download_url": "https://github.com/dl/fnmusic-ext-2.12.4-0.fpk",
                "size": 5000,
                "digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            }
        ],
    }
    gt_release_no_fpk = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "source": "gitee",
        "assets": [
            {
                "name": "fnmusic-ext-2.12.4-0.fpk.sha256",
                "browser_download_url": "https://gitee.com/dl/fnmusic-ext-2.12.4-0.fpk.sha256",
            }
        ],
    }

    def mock_fetch(url: str):
        if "gitee.com" in url:
            return gt_release_no_fpk
        return gh_release

    snap = resolve_target("v2.12.4", arch="amd64", http_getter=mock_fetch)
    assert snap is not None
    assert snap.source == "github"
    assert len(snap.candidates) == 1
    assert snap.candidates[0]["source"] == "github"


def test_download_and_verify_success_and_sha_mismatch(tmp_path):
    content = b"fake-fpk-content-123456"
    correct_sha = hashlib.sha256(content).hexdigest()
    bad_sha = "0" * 64

    candidate = {
        "download_url": "https://example.com/test.fpk",
        "size": len(content),
        "sha256": correct_sha,
    }
    target = TargetSnapshot(
        target_id="test-id",
        tag="v2.12.4",
        formal_base="2.12.4",
        install_version="2.12.4-0",
        architecture="amd64",
        asset_name="test.fpk",
        download_url="https://example.com/test.fpk",
        size=len(content),
        sha256=correct_sha,
        sha256_url="",
        source="github",
        candidates=[candidate],
        created_at=100.0,
        expires_at=700.0,
    )

    class MockResp:
        status = 200
        headers = {"Content-Length": str(len(content))}
        def __init__(self):
            self.bytes = content
            self.pos = 0
        def read(self, n=65536):
            chunk = self.bytes[self.pos : self.pos + n]
            self.pos += len(chunk)
            return chunk
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    dest = tmp_path / "test.fpk"

    with patch("urllib.request.urlopen", return_value=MockResp()):
        ok, err = _download_and_verify(candidate, target, dest)
    assert ok is True
    assert dest.is_file()
    assert dest.read_bytes() == content

    # SHA mismatch
    bad_candidate = dict(candidate, sha256=bad_sha)
    dest_bad = tmp_path / "bad.fpk"
    with patch("urllib.request.urlopen", return_value=MockResp()):
        ok, err = _download_and_verify(bad_candidate, target, dest_bad)
    assert ok is False
    assert "不匹配" in err
    assert not dest_bad.exists()

    no_digest = dict(candidate, sha256="")
    target.sha256 = ""
    dest_unknown = tmp_path / "unknown.fpk"
    with patch("urllib.request.urlopen", return_value=MockResp()):
        ok, err = _download_and_verify(no_digest, target, dest_unknown)
    assert ok is False
    assert "SHA256" in err
    assert not dest_unknown.exists()


def test_create_data_backup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("FNMUSIC_TEST=1\n")
    (repo / "config").mkdir()
    (repo / "config" / "sub.conf").write_text("cfg\n")
    (repo / "playlist_tracks").mkdir()
    (repo / "playlist_tracks" / "pl1.json").write_text("{}\n")

    dest_tar = tmp_path / "backup.tar.gz"
    assert create_data_backup(dest_tar, repo_dir=repo) is True
    assert dest_tar.is_file()

    with tarfile.open(dest_tar, "r:gz") as tar:
        names = tar.getnames()
        assert ".env" in names
        assert any("sub.conf" in n for n in names)
        assert any("pl1.json" in n for n in names)


def test_create_data_backup_returns_failure_when_archive_cannot_be_created(tmp_path):
    with patch("tarfile.open", side_effect=OSError("disk unavailable")):
        assert create_data_backup(tmp_path / "backup.tar.gz", repo_dir=tmp_path) is False


def test_native_app_center_client_flow():
    from proxy.self_update import NativeAppCenterClient

    client = NativeAppCenterClient("/fake/socket.sock")

    with patch.object(client, "_request") as mock_req:
        mock_req.return_value = {"code": 10030, "msg": ""}
        assert client.check_capability() is True

        mock_req.return_value = {"code": 500, "msg": "err"}
        assert client.check_capability() is False

    with patch.object(client, "_request") as mock_req:
        # stage_fpk
        mock_req.side_effect = [
            {"code": 0, "data": {"downloadTaskId": "task-1"}},
            {"code": 0, "data": {"status": 2, "appName": "fnmusic-ext", "version": "2.12.4-0"}},
        ]
        staged = client.stage_fpk("/path/to/pkg.fpk")
        assert staged["appName"] == "fnmusic-ext"

    with patch.object(client, "_request") as mock_req:
        # get_update_info
        mock_req.return_value = {
            "code": 0,
            "data": {"wizardInfo": {"installedVolumeID": 2}},
        }
        vol = client.get_update_info("fnmusic-ext", "2.12.4-0")
        assert vol == 2

    with patch.object(client, "_request") as mock_req:
        # submit_update
        mock_req.return_value = {"code": 0, "data": {"taskId": "update-task-99"}}
        task_id = client.submit_update("fnmusic-ext", "2.12.4-0", 2)
        assert task_id == "update-task-99"

    with patch.object(client, "_request") as mock_req:
        # wait_task
        mock_req.return_value = {"code": 0, "data": {"status": 2}}
        status_code, msg = client.wait_task("update-task-99")
        assert status_code == 2

    with patch.object(client, "_request") as mock_req:
        # verify_installed
        mock_req.return_value = {
            "code": 0,
            "data": {
                "list": [
                    {"appName": "fnmusic-ext", "version": "2.12.4-0"},
                    {"appName": "other-app", "version": "1.0"},
                ]
            },
        }
        ok, ver = client.verify_installed("fnmusic-ext", "2.12.4-0")
        assert ok is True
        assert ver == "2.12.4-0"
        ok, ver = client.verify_installed("fnmusic-ext", "2.12.4-1")
        assert ok is False
        assert "版本不一致" in ver


def test_job_lifecycle_and_deduplication(tmp_path, monkeypatch):
    import proxy.self_update as su

    monkeypatch.setattr(su, "get_job_base_dir", lambda: tmp_path / "jobs")
    su._ACTIVE_JOB = None
    su._TARGET_CACHE.clear()

    # Unknown target
    status, resp = start_update_job("nonexistent-target")
    assert status == 409
    assert "过期" in resp["error"] or "不存在" in resp["error"]

    snap = TargetSnapshot(
        target_id="snap-1",
        tag="v2.12.4",
        formal_base="2.12.4",
        install_version="2.12.4-0",
        architecture="amd64",
        asset_name="fnmusic-ext-2.12.4-0.fpk",
        download_url="https://example.com/fpk",
        size=100,
        sha256="",
        sha256_url="",
        source="github",
        candidates=[],
        created_at=time.time(),
        expires_at=time.time() + 600.0,
    )
    su._TARGET_CACHE["snap-1"] = snap

    # Mock subprocess.Popen so worker doesn't run asynchronously in test
    with patch("shutil.which", return_value=None), patch("subprocess.Popen") as mock_popen:
        status, resp = start_update_job("snap-1")
        assert status == 202
        assert resp["ok"] is True
        job_data = resp["job"]
        job_id = job_data["id"]
        assert job_data["install_version"] == "2.12.4-0"

        # Re-submitting the same target while queued/running returns 202 with same job
        status2, resp2 = start_update_job("snap-1")
        assert status2 == 202
        assert resp2["job"]["id"] == job_id

        # Submitting different target while active returns 409
        snap2 = TargetSnapshot(
            target_id="snap-2",
            tag="v2.12.5",
            formal_base="2.12.5",
            install_version="2.12.5-0",
            architecture="amd64",
            asset_name="fnmusic-ext-2.12.5-0.fpk",
            download_url="https://example.com/fpk2",
            size=100,
            sha256="",
            sha256_url="",
            source="github",
            candidates=[],
            created_at=time.time(),
            expires_at=time.time() + 600.0,
        )
        su._TARGET_CACHE["snap-2"] = snap2
        status3, resp3 = start_update_job("snap-2")
        assert status3 == 409
        assert "已有其他更新任务" in resp3["error"]

        finished = su._read_job_state(tmp_path / "jobs" / job_id)
        finished.status = "completed"
        su._save_job_state(tmp_path / "jobs" / job_id, finished)
        status4, resp4 = start_update_job("snap-2")
        assert status4 == 202
        assert resp4["job"]["id"] != job_id
        job_id = resp4["job"]["id"]

    # Retrieve job from disk
    su._ACTIVE_JOB = None
    recovered = su.get_active_job()
    assert recovered is not None
    assert recovered.id == job_id


def test_start_update_recovers_disk_job_and_falls_back_when_systemd_rejects(tmp_path, monkeypatch):
    import proxy.self_update as su

    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr(su, "get_job_base_dir", lambda: jobs_dir)
    monkeypatch.setattr(su, "_ACTIVE_JOB", None)
    monkeypatch.setattr(su, "_TARGET_CACHE", {})
    monkeypatch.setattr(su.os, "getuid", lambda: 0)
    snap = TargetSnapshot(
        target_id="snap", tag="v2.12.4", formal_base="2.12.4",
        install_version="2.12.4-0", architecture="amd64",
        asset_name="fnmusic-ext-2.12.4-0.fpk", download_url="https://example.com/fpk",
        size=100, sha256="", sha256_url="", source="github", candidates=[],
        created_at=time.time(), expires_at=time.time() + 600,
    )
    su._TARGET_CACHE[snap.target_id] = snap
    with patch("shutil.which", return_value="/usr/bin/systemd-run"), \
         patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)) as systemd, \
         patch("subprocess.Popen") as detached:
        status, response = start_update_job(snap.target_id)
        assert status == 202
        assert systemd.call_count == 1
        assert any(arg.startswith("--working-directory=") for arg in systemd.call_args.args[0])
        assert detached.call_count == 1
        assert detached.call_args.kwargs["start_new_session"] is True
        job_id = response["job"]["id"]
        su._ACTIVE_JOB = None
        status, response = start_update_job(snap.target_id)
        assert status == 202
        assert response["job"]["id"] == job_id
        assert detached.call_count == 1


@pytest.mark.parametrize("worker_started", [False, True])
def test_systemd_timeout_does_not_overwrite_or_duplicate_job(tmp_path, monkeypatch, worker_started):
    import proxy.self_update as su

    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr(su, "get_job_base_dir", lambda: jobs_dir)
    monkeypatch.setattr(su, "_ACTIVE_JOB", None)
    monkeypatch.setattr(su, "_TARGET_CACHE", {})
    monkeypatch.setattr(su.os, "getuid", lambda: 0)
    snap = TargetSnapshot(
        target_id="snap", tag="v2.12.4", formal_base="2.12.4",
        install_version="2.12.4-0", architecture="amd64",
        asset_name="fnmusic-ext-2.12.4-0.fpk", download_url="https://example.com/fpk",
        size=100, sha256="", sha256_url="", source="github", candidates=[],
        created_at=time.time(), expires_at=time.time() + 600,
    )
    su._TARGET_CACHE[snap.target_id] = snap

    def timeout(*args, **kwargs):
        if worker_started:
            job_dir = next(jobs_dir.iterdir())
            job = su._read_job_state(job_dir)
            job.status = "running"
            su._save_job_state(job_dir, job)
        raise subprocess.TimeoutExpired(args[0], 15)

    with patch("shutil.which", return_value="/usr/bin/systemd-run"), \
         patch("subprocess.run", side_effect=timeout) as systemd, \
         patch("subprocess.Popen") as detached:
        status, response = start_update_job(snap.target_id)
        assert status == 503
        assert "超时" in response["error"]
        su._ACTIVE_JOB = None
        active = su.get_active_job()
        assert active is not None
        assert active.status == ("running" if worker_started else "queued")
        status, response = start_update_job(snap.target_id)
        assert status == 202
        assert response["job"]["id"] == active.id
        assert systemd.call_count == 1
        detached.assert_not_called()


def test_execute_job_does_not_report_install_success_without_daemon(tmp_path, monkeypatch):
    import proxy.self_update as su

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    monkeypatch.setattr(su, "DAEMON_SOCKET", str(tmp_path / "missing.sock"))
    snap = TargetSnapshot(
        target_id="snap", tag="v2.12.4", formal_base="2.12.4",
        install_version="2.12.4-0", architecture="amd64",
        asset_name="fnmusic-ext-2.12.4-0.fpk", download_url="https://example.com/fpk",
        size=100, sha256="", sha256_url="", source="github", candidates=[{}],
        created_at=time.time(), expires_at=time.time() + 600,
    )
    job = UpdateJob(
        id="job", target_id="snap", tag=snap.tag, install_version=snap.install_version,
        architecture="amd64", status="queued", phase="downloading", source="",
        downloaded_bytes=0, total_bytes=0, daemon_task_id="", error_code="", message="",
        created_at=time.time(), updated_at=time.time(),
    )
    su._save_job_state(job_dir, job)
    monkeypatch.setattr(su, "get_target_snapshot", lambda target_id: snap)
    monkeypatch.setattr(su, "_download_and_verify", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(su, "create_data_backup", lambda *args: True)
    assert su.execute_job(job_dir) != 0
    result = su._read_job_state(job_dir)
    assert result.status == "failed"
    assert "未" in result.message and "安装" in result.message


def test_execute_job_rejects_mismatched_staged_revision(tmp_path, monkeypatch):
    import proxy.self_update as su

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    daemon = tmp_path / "daemon.sock"
    daemon.touch()
    monkeypatch.setattr(su, "DAEMON_SOCKET", str(daemon))
    snap = TargetSnapshot(
        target_id="snap", tag="v2.12.4", formal_base="2.12.4",
        install_version="2.12.4-1", architecture="amd64",
        asset_name="fnmusic-ext-2.12.4-1.fpk", download_url="https://example.com/fpk",
        size=100, sha256="", sha256_url="", source="github", candidates=[{}],
        created_at=time.time(), expires_at=time.time() + 600,
    )
    job = UpdateJob(
        id="job", target_id="snap", tag=snap.tag, install_version=snap.install_version,
        architecture="amd64", status="queued", phase="downloading", source="",
        downloaded_bytes=0, total_bytes=0, daemon_task_id="", error_code="", message="",
        created_at=time.time(), updated_at=time.time(),
    )
    su._save_job_state(job_dir, job)
    monkeypatch.setattr(su, "get_target_snapshot", lambda target_id: snap)
    monkeypatch.setattr(su, "_download_and_verify", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(su, "create_data_backup", lambda *args: True)
    client = MagicMock()
    client.stage_fpk.return_value = {"appName": "fnmusic-ext", "version": "2.12.4-0"}
    monkeypatch.setattr(su, "NativeAppCenterClient", lambda socket_path: client)
    assert su.execute_job(job_dir) == 5
    client.get_update_info.assert_not_called()
    assert "版本不匹配" in su._read_job_state(job_dir).message
