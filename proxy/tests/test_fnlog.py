"""proxy/fnlog.py 统一日志模块测试。"""
import logging
import os
import time

import pytest

from proxy import fnlog


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path / "logs"


def test_log_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LOG_DIR", str(tmp_path / "x"))
    assert fnlog.log_dir() == tmp_path / "x"
    monkeypatch.delenv("FNMUSIC_LOG_DIR")
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path / "home"))
    assert fnlog.log_dir() == tmp_path / "home" / "logs"
    monkeypatch.delenv("FNMUSIC_HOME")
    # 兜底：仓库根/logs（含 proxy 目录与 VERSION 的层级）
    d = fnlog.log_dir()
    assert d.name == "logs"
    assert (d.parent / "proxy").is_dir()
    assert (d.parent / "VERSION").exists()


def test_setup_logging_writes_file_and_idempotent(log_dir):
    lg1 = fnlog.setup_logging("fnlog_test_a", "unittest_a")
    lg1.info("hello fnlog")
    for h in lg1.handlers:
        h.flush()
    assert (log_dir / "unittest_a.log").exists()
    assert "hello fnlog" in (log_dir / "unittest_a.log").read_text(encoding="utf-8")
    n_handlers = len(lg1.handlers)
    fnlog.setup_logging("fnlog_test_a", "unittest_a")
    assert len(lg1.handlers) == n_handlers  # 幂等，不重复挂 handler


def test_rotation_keeps_three_days_max(log_dir):
    directory = log_dir
    directory.mkdir(parents=True, exist_ok=True)
    handler = fnlog._BoundedTimedRotatingFileHandler(
        directory / "rot.log", when="S", interval=1, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("fnlog_test_rot")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(handler)
    try:
        for i in range(5):
            lg.info("record %d", i)
            handler.flush()
            handler.rolloverAt = time.time() - 2  # 强制下一次 emit 触发按天轮转
    finally:
        handler.close()
        lg.removeHandler(handler)
    files = sorted(p.name for p in directory.glob("rot.log*"))
    assert 1 <= len(files) <= 3  # 当前 + backupCount=2 = 恰好 3 天
    assert "rot.log" in files


def test_purge_stale_removes_old_files_and_logzip(log_dir):
    directory = log_dir
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    fresh = directory / "proxy.log"
    fresh.write_text("x", encoding="utf-8")
    old_log = directory / "install.log"
    old_log.write_text("x", encoding="utf-8")
    os.utime(old_log, (now - 4 * 86400, now - 4 * 86400))
    old_zip = directory / "20260101000000.logzip"
    old_zip.write_text("x", encoding="utf-8")
    os.utime(old_zip, (now - 4 * 86400, now - 4 * 86400))
    removed = fnlog.purge_stale(days=3, now=now)
    assert fresh.exists()
    assert not old_log.exists()
    assert not old_zip.exists()
    assert set(removed) == {"install.log", "20260101000000.logzip"}


def test_redact_masks_secret_values():
    text = "FNMUSIC_LLM_API_KEY='sk-abc123' cookie: a=b; token=xyz Authorization: Bearer T0"
    out = fnlog.redact(text)
    assert "sk-abc123" not in out
    assert "xyz" not in out
    assert "T0" not in out
    assert "API_KEY=***" in out
    safe = fnlog.redact("FNMUSIC_QUALITY_MODE=high port=8774")
    assert safe == "FNMUSIC_QUALITY_MODE=high port=8774"


def test_diag_format_truncates_and_redacts():
    lg = logging.getLogger("fnlog_test_diag")
    events: list[str] = []
    original = lg.info

    def capture(msg, *args, **kw):
        events.append(msg % args if args else msg)

    lg.info = capture  # type: ignore[method-assign]
    try:
        fnlog.diag(lg, "stream_abort", guid="g1", api_key="secret123", extra="x" * 500)
    finally:
        lg.info = original  # type: ignore[method-assign]
    line = events[0]
    assert line.startswith("[diag] stream_abort ")
    assert "secret123" not in line
    assert "api_key=***" in line
    assert len(line) <= len("[diag] stream_abort ") + 300 + 5


def test_env_snapshot_lines_contexts(log_dir):
    for ctx in ("host", "container"):
        lines = fnlog.env_snapshot_lines(ctx)
        assert lines
        assert any(f"context={ctx}" in ln for ln in lines)
        assert any(ln.startswith("[env] hostname=") for ln in lines)
    host_lines = fnlog.env_snapshot_lines("host")
    assert any("fnos_version=" in ln for ln in host_lines)
    container_lines = fnlog.env_snapshot_lines("container")
    assert not any("fnos_version=" in ln for ln in container_lines)


def test_write_env_snapshot(log_dir):
    assert fnlog.write_env_snapshot("host")
    content = (log_dir / fnlog.SNAPSHOT_FILE).read_text(encoding="utf-8")
    assert "[env] context=host" in content
