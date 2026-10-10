"""proxy/fnlog.py 统一日志模块测试。"""
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from proxy import fnlog


@pytest.fixture(autouse=True)
def isolate_logging(monkeypatch):
    for key in ("FNMUSIC_LOG_DIR", "FNMUSIC_HOME", "WEBUI_REPO_DIR"):
        monkeypatch.delenv(key, raising=False)
    original_setup = fnlog._SET_UP.copy()
    yield
    for key in fnlog._SET_UP - original_setup:
        logger = logging.getLogger(key.rsplit(":", 1)[0])
        for handler in logger.handlers[:]:
            if isinstance(handler, fnlog._BoundedTimedRotatingFileHandler):
                logger.removeHandler(handler)
                handler.close()
    fnlog._SET_UP.clear()
    fnlog._SET_UP.update(original_setup)


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


def test_log_dir_shared_repo_and_docker_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_REPO_DIR", str(tmp_path / "repo"))
    assert fnlog.log_dir() == tmp_path / "repo" / "logs"
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path / "home"))
    assert fnlog.log_dir() == tmp_path / "home" / "logs"
    monkeypatch.setenv("FNMUSIC_LOG_DIR", str(tmp_path / "explicit"))
    assert fnlog.log_dir() == tmp_path / "explicit"
    monkeypatch.delenv("FNMUSIC_LOG_DIR")
    monkeypatch.delenv("FNMUSIC_HOME")
    monkeypatch.delenv("WEBUI_REPO_DIR")
    monkeypatch.setattr(fnlog, "__file__", str(tmp_path / "app" / "fnlog.py"))
    assert fnlog.log_dir() == Path("/repo/logs")


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


def _record(message):
    return logging.LogRecord("fnlog_rotation", logging.INFO, __file__, 0, message, (), None)


@pytest.mark.parametrize("delay", [False, True])
def test_size_rollover_repeated_same_day_is_bounded_and_preserves_latest(log_dir, delay):
    log_dir.mkdir()
    handler = fnlog._BoundedTimedRotatingFileHandler(
        log_dir / "size.log", when="midnight", backupCount=2,
        max_bytes=12, encoding="utf-8", delay=delay,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    deadline = handler.rolloverAt
    try:
        for i in range(10):
            handler.handle(_record(f"record-{i:02d}"))
            handler.flush()
            files = sorted(log_dir.glob("size.log*"))
            assert len(files) <= 3
            assert (log_dir / "size.log").read_text() == f"record-{i:02d}\n"
            assert handler.rolloverAt == deadline  # Size does not defer daily rotation.
            contents = {p.read_text() for p in files}
            assert contents == {f"record-{j:02d}\n" for j in range(max(0, i - 2), i + 1)}
        # Reopening a handler must not reuse an existing same-day archive name.
        handler.close()
        handler = fnlog._BoundedTimedRotatingFileHandler(
            log_dir / "size.log", when="midnight", backupCount=2,
            max_bytes=12, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.handle(_record("record-10"))
        handler.flush()
        assert {p.read_text() for p in log_dir.glob("size.log*")} == {
            "record-08\n", "record-09\n", "record-10\n"
        }
    finally:
        handler.close()


def test_combined_daily_and_size_rollover(log_dir, monkeypatch):
    log_dir.mkdir()
    now = int(time.time())
    monkeypatch.setattr(fnlog.time, "time", lambda: now)
    handler = fnlog._BoundedTimedRotatingFileHandler(
        log_dir / "daily.log", when="midnight", backupCount=2,
        max_bytes=10, encoding="utf-8", utc=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.handle(_record("first"))
        handler.rolloverAt = now
        handler.handle(_record("second"))  # Both daily and size limits met.
        next_midnight = handler.rolloverAt
        assert next_midnight > now
        handler.handle(_record("third"))  # Same-day size rollover after daily rollover.
        handler.handle(_record("fourth"))
        handler.flush()
        assert handler.rolloverAt == next_midnight
        assert {p.read_text() for p in log_dir.glob("daily.log*")} == {
            "second\n", "third\n", "fourth\n"
        }
    finally:
        handler.close()


def test_size_rollover_counts_utf8_bytes_and_ignores_empty_file(log_dir):
    log_dir.mkdir()
    handler = fnlog._BoundedTimedRotatingFileHandler(
        log_dir / "utf8.log", when="midnight", backupCount=2,
        max_bytes=10, encoding="utf-8", delay=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.handle(_record("你好"))  # Seven UTF-8 bytes.
        handler.handle(_record("世界"))
        handler.flush()
        assert {p.read_text() for p in log_dir.glob("utf8.log*")} == {"你好\n", "世界\n"}
        handler.handle(_record("x" * 100))  # One oversized record, not empty archives.
        handler.flush()
        assert all(p.stat().st_size for p in log_dir.glob("utf8.log*"))
    finally:
        handler.close()


@pytest.mark.parametrize("backup_count", [0, 1, 2])
def test_same_day_archive_limit_zero_is_not_unlimited(log_dir, backup_count):
    log_dir.mkdir()
    handler = fnlog._BoundedTimedRotatingFileHandler(
        log_dir / "bounded.log", when="midnight", backupCount=backup_count,
        max_bytes=6, encoding="utf-8",
    )
    try:
        for i in range(8):
            handler.handle(_record(f"line{i}"))
            handler.flush()
            assert len(list(log_dir.glob("bounded.log*"))) <= backup_count + 1
    finally:
        handler.close()


@pytest.mark.parametrize("date,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_midnight_rollover_remains_midnight_across_dst(log_dir, monkeypatch, date, hours):
    if not hasattr(time, "tzset"):
        pytest.skip("requires POSIX timezone support")
    old_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")
    time.tzset()
    log_dir.mkdir()
    handler = None
    try:
        now = int(time.mktime(time.strptime(date, "%Y-%m-%d")))
        monkeypatch.setattr(fnlog.time, "time", lambda: now)
        handler = fnlog._BoundedTimedRotatingFileHandler(
            log_dir / "dst.log", when="midnight", backupCount=2, encoding="utf-8", delay=True,
        )
        assert handler.rolloverAt - now == hours * 3600  # Initial schedule too.
        handler.rolloverAt = now
        handler.handle(_record("midnight"))
        assert handler.rolloverAt - now == hours * 3600
        assert time.localtime(handler.rolloverAt)[3:6] == (0, 0, 0)
    finally:
        if handler:
            handler.close()
        if old_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_timezone
        time.tzset()


def test_archive_cleanup_includes_legacy_dates_but_not_unrelated_files(log_dir):
    log_dir.mkdir()
    for name in ("legacy.log.2020-01-01", "legacy.log.2020-01-02.00000001",
                 "legacy.log.notes", "legacy.log.2020-01-03.txt"):
        (log_dir / name).write_text(name)
    handler = fnlog._BoundedTimedRotatingFileHandler(
        log_dir / "legacy.log", when="midnight", backupCount=2, encoding="utf-8",
    )
    try:
        handler.handle(_record("fresh"))
        handler.doRollover()
        assert not (log_dir / "legacy.log.2020-01-01").exists()
        assert (log_dir / "legacy.log.2020-01-02.00000001").exists()
        assert (log_dir / "legacy.log.notes").exists()
        assert (log_dir / "legacy.log.2020-01-03.txt").exists()
    finally:
        handler.close()


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


@pytest.mark.parametrize("text,secrets", [
    ("Authorization: Bearer " + "a" * 800 + "AUTH_TAIL", ["a" * 20, "AUTH_TAIL"]),
    ('"Authorization": "Bearer ' + "b" * 800 + 'AUTH_TAIL", "status": 200', ["b" * 20, "AUTH_TAIL"]),
    ('API_KEY="' + "k" * 800 + 'KEY_TAIL" quality=high', ["k" * 20, "KEY_TAIL"]),
    ("password=" + "p" * 800 + "PASS_TAIL mode=high", ["p" * 20, "PASS_TAIL"]),
    ("Cookie: first=COOKIE_ONE; second=COOKIE_TWO; third=COOKIE_THREE", ["COOKIE_ONE", "COOKIE_TWO", "COOKIE_THREE"]),
    ('{"cookie": "a=COOKIE_ONE; b=COOKIE_TWO", "ok": true}', ["COOKIE_ONE", "COOKIE_TWO"]),
    ('cookie="a=COOKIE_ONE"; b=COOKIE_TWO; c="COOKIE_THREE"', ["COOKIE_ONE", "COOKIE_TWO", "COOKIE_THREE"]),
    ('Authorization: "Bearer QUOTED_AUTH" AUTH_TAIL', ["QUOTED_AUTH", "AUTH_TAIL"]),
    ('Authorization: "Bearer FIRST_LINE\nSECOND_LINE"', ["FIRST_LINE", "SECOND_LINE"]),
    ('cookie="first=FIRST_COOKIE;\nsecond=SECOND_COOKIE"', ["FIRST_COOKIE", "SECOND_COOKIE"]),
    ('{"api-key":"JSON_KEY", "access_token": "JSON_TOKEN", "password": "JSON_PASS"}', ["JSON_KEY", "JSON_TOKEN", "JSON_PASS"]),
    ("{'authorization': 'Bearer PYTHON_AUTH', 'secret': 'PYTHON_SECRET'}", ["PYTHON_AUTH", "PYTHON_SECRET"]),
    ('{"token": "escaped\\\"SECRET_TAIL", "safe": "ok"}', ["escaped", "SECRET_TAIL"]),
    ('{"secret": {"one": "NESTED_ONE", "two": ["NESTED_TWO"]}}', ["NESTED_ONE", "NESTED_TWO"]),
    ("token=unterminated\npassword='UNCLOSED_SECRET", ["unterminated", "UNCLOSED_SECRET"]),
    ('endpoint="https://host/path?access_token=URL_TOKEN&api_key=URL_KEY"', ["URL_TOKEN", "URL_KEY"]),
    ('{"endpoint": "https://host/path?token=JSON_URL_TOKEN&password=JSON_URL_PASS"}', ["JSON_URL_TOKEN", "JSON_URL_PASS"]),
    ("token=SECRET?TAIL#FRAGMENT", ["SECRET", "TAIL", "FRAGMENT"]),
])
def test_redact_complete_secret_formats(text, secrets):
    output = fnlog.redact(text)
    assert "***" in output
    for secret in secrets:
        assert secret not in output


@pytest.mark.parametrize("depth", [1, 2, 10])
def test_redact_percent_encoded_nested_url_tokens(depth):
    url = "https://internal/path?access_token=NESTED_TOKEN&api_key=NESTED_KEY"
    for _ in range(depth):
        url = quote(url, safe="")
    output = fnlog.redact(f'endpoint="https://public/redirect?next={url}"')
    assert "NESTED_TOKEN" not in output
    assert "NESTED_KEY" not in output
    assert "***" in output


def test_redact_encoded_credentials_does_not_leak_decoded_delimiter_tails():
    output = fnlog.redact('endpoint="https://host/?token=HEAD%20SECRET_TAIL%26AFTER"')
    assert all(secret not in output for secret in ("HEAD", "SECRET_TAIL", "AFTER"))
    safe = 'endpoint="https://host/?q=hello%20world" port=8774'
    assert fnlog.redact(safe) == safe


def test_redact_preserves_json_adjacent_safe_fields_and_lines():
    text = '{"Authorization": "Bearer TOP_SECRET", "status": 200}\nport=8774'
    output = fnlog.redact(text)
    assert "TOP_SECRET" not in output
    assert '"status": 200' in output
    assert "port=8774" in output


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


def test_log_and_snapshot_permissions_under_umask_0077(log_dir, monkeypatch):
    log_dir.mkdir()
    group = 1000 if os.geteuid() == 0 or 1000 in os.getgroups() else os.getgid()
    os.chown(log_dir, -1, group)
    log_dir.chmod(0o2770)
    monkeypatch.setattr(fnlog, "env_snapshot_lines", lambda _: ["[env] token=SNAPSHOT_SECRET"])
    previous_umask = os.umask(0o077)
    handler = None
    try:
        handler = fnlog._BoundedTimedRotatingFileHandler(
            log_dir / "perms.log", when="midnight", backupCount=2,
            max_bytes=10, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        for message in ("first", "second", "third", "fourth"):
            handler.handle(_record(message))
        handler.flush()
        assert fnlog.write_env_snapshot()
        for path in log_dir.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o640
            assert path.stat().st_gid == group
            assert path.stat().st_uid == os.geteuid()
        assert "SNAPSHOT_SECRET" not in (log_dir / fnlog.SNAPSHOT_FILE).read_text()
        # Reopen repairs an owned legacy log created using root's restrictive umask.
        handler.close()
        (log_dir / "perms.log").chmod(0o600)
        handler = fnlog._BoundedTimedRotatingFileHandler(
            log_dir / "perms.log", when="midnight", backupCount=2, encoding="utf-8",
        )
        assert stat.S_IMODE((log_dir / "perms.log").stat().st_mode) == 0o640
        (log_dir / fnlog.SNAPSHOT_FILE).chmod(0o600)
        assert fnlog.write_env_snapshot()
        assert stat.S_IMODE((log_dir / fnlog.SNAPSHOT_FILE).stat().st_mode) == 0o640
    finally:
        if handler is not None:
            handler.close()
        os.umask(previous_umask)


def test_root_created_files_are_readable_to_container_uid_1000(log_dir, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("root-to-uid1000 permissions check runs in the Python 3.13 test container")
    log_dir.mkdir()
    # tmp_path's parents are deliberately private; permit traversal for the
    # subprocess, without exposing file contents or changing repository paths.
    for parent in log_dir.parents:
        if parent == Path("/tmp"):
            break
        parent.chmod(parent.stat().st_mode | stat.S_IXOTH)
    os.chown(log_dir, 0, 1000)
    log_dir.chmod(0o2770)
    monkeypatch.setattr(fnlog, "env_snapshot_lines", lambda _: ["[env] context=host"])
    previous_umask = os.umask(0o077)
    try:
        logger = fnlog.setup_logging("fnlog_test_root_group", "root")
        logger.info("ROOT_LOG_CONTENT")
        for handler in logger.handlers:
            handler.flush()
            handler.doRollover()
        logger.info("ACTIVE_LOG_CONTENT")
        assert fnlog.write_env_snapshot()
        for path in log_dir.iterdir():
            assert path.stat().st_uid == 0
            assert path.stat().st_gid == 1000
            assert stat.S_IMODE(path.stat().st_mode) == 0o640

        def container_identity():
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)

        result = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; import sys; "
             "p=Path(sys.argv[1]); "
             "assert all(f.read_text() for f in p.iterdir()); "
             "(p/'container.log').write_text('container')", str(log_dir)],
            preexec_fn=container_identity, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_log_and_snapshot_paths_rejected_without_modifying_targets(log_dir, kind):
    log_dir.mkdir()
    target = log_dir / "unrelated"
    target.write_text("DO_NOT_TOUCH")
    target.chmod(0o600)
    for name in ("unsafe.log", fnlog.SNAPSHOT_FILE):
        path = log_dir / name
        if kind == "symlink":
            path.symlink_to(target)
        elif kind == "hardlink":
            os.link(target, path)
        else:
            os.mkfifo(path, 0o600)
    logger = fnlog.setup_logging("fnlog_test_unsafe", "unsafe")
    logger.info("file failure is nonfatal")
    assert not any(isinstance(h, fnlog._BoundedTimedRotatingFileHandler) for h in logger.handlers)
    assert not fnlog.write_env_snapshot()
    assert target.read_text() == "DO_NOT_TOUCH"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_foreign_owned_file_permissions_are_not_changed(log_dir, monkeypatch):
    log_dir.mkdir()
    target = log_dir / "foreign.log"
    target.write_text("DO_NOT_TOUCH")
    target.chmod(0o600)
    # Simulate another owner without requiring chown/root in CI.
    monkeypatch.setattr(fnlog.os, "geteuid", lambda: target.stat().st_uid + 1)
    with pytest.raises(PermissionError):
        fnlog._open_log_file(target, "w")
    assert target.read_text() == "DO_NOT_TOUCH"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.chmod(0o640)
    with fnlog._open_log_file(target) as stream:
        stream.write(" APPEND")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_file_io_failures_are_nonfatal_and_do_not_print_secret_records(log_dir, monkeypatch, capsys):
    log_dir.mkdir()
    handler = fnlog._BoundedTimedRotatingFileHandler(
        log_dir / "failure.log", when="midnight", backupCount=2,
        max_bytes=8, encoding="utf-8",
    )
    try:
        handler.handle(_record("first"))
        handler.flush()

        def fail(*args, **kwargs):
            raise OSError("disk unavailable")

        monkeypatch.setattr(handler, "rotate", fail)
        handler.handle(_record("password=DO_NOT_PRINT"))
        assert "DO_NOT_PRINT" not in capsys.readouterr().err
        monkeypatch.setattr(fnlog, "_open_log_file", fail)
        assert not fnlog.write_env_snapshot()
        logger = fnlog.setup_logging("fnlog_test_io_failure", "failure2")
        logger.info("still alive")
    finally:
        handler.close()
