from datetime import datetime, timezone

from source_cpu import (
    ProcCpuSample,
    SourceCpuMonitor,
    parse_proc_stat,
    read_proc_stat,
)


def _monitor():
    return SourceCpuMonitor(
        threshold_percent=50,
        required_duration_s=180,
        sample_interval_s=5,
        max_gap_s=15,
        clock_ticks=100,
        wall_clock=lambda: datetime(2026, 11, 14, tzinfo=timezone.utc),
    )


def _sample(cpu_seconds: float, *, pid: int = 123, start: int = 99):
    return ProcCpuSample(pid=pid, start_time_ticks=start, cpu_ticks=round(cpu_seconds * 100))


def _feed(monitor, samples, *, start=0.0):
    usage = None
    for index, cpu_seconds in enumerate(samples):
        usage = monitor.sample(
            "run-1",
            _sample(cpu_seconds),
            now=start + index * 5.0,
        )
    return usage


def test_proc_stat_parser_handles_spaces_and_parentheses_in_comm():
    text = "123 (worker (source)) S " + " ".join(str(i) for i in range(1, 20))
    sample = parse_proc_stat(text, clock_ticks=100)
    assert sample.pid == 123
    assert sample.cpu_ticks == 23
    assert sample.start_time_ticks == 19


def test_read_proc_stat_returns_none_for_missing_or_malformed(tmp_path):
    assert read_proc_stat(999999, proc_root=tmp_path) is None
    proc = tmp_path / "42"
    proc.mkdir()
    (proc / "stat").write_text("not a stat", encoding="utf-8")
    assert read_proc_stat(42, proc_root=tmp_path) is None


def test_exactly_fifty_percent_never_triggers():
    monitor = _monitor()
    monitor.register("run-1", 123, 99)
    samples = [i * 2.5 for i in range(61)]  # 2.5 CPU seconds per 5-second window.
    usage = _feed(monitor, samples)
    assert usage["cpu_percent"] == 50.0
    assert usage["high_duration_s"] == 0.0
    assert usage["abnormal"] is False
    assert usage["reason"] == ""


def test_179_seconds_does_not_trigger_and_180_triggers():
    monitor = _monitor()
    monitor.register("run-1", 123, 99)
    monitor.sample("run-1", _sample(0), now=0)
    now = 0.0
    cpu = 0.0
    for _ in range(35):
        now += 5.0
        cpu += 3.0  # 60%
        usage = monitor.sample("run-1", _sample(cpu), now=now)
    assert usage["high_duration_s"] == 175.0
    assert usage["abnormal"] is False

    now += 4.0
    cpu += 2.4  # Still 60%; total 179 seconds.
    usage = monitor.sample("run-1", _sample(cpu), now=now)
    assert usage["high_duration_s"] == 179.0
    assert usage["abnormal"] is False

    now += 1.0
    cpu += 0.6
    usage = monitor.sample("run-1", _sample(cpu), now=now)
    assert usage["high_duration_s"] == 180.0
    assert usage["abnormal"] is True
    assert usage["reason"] == "sustained_source_cpu"
    assert usage["detected_at"] == "2026-11-14T00:00:00+00:00"


def test_normal_window_resets_streak_before_trigger():
    monitor = _monitor()
    monitor.register("run-1", 123, 99)
    monitor.sample("run-1", _sample(0), now=0)
    now = 0.0
    cpu = 0.0
    for _ in range(30):
        now += 5.0
        cpu += 3.0
        monitor.sample("run-1", _sample(cpu), now=now)
    now += 5.0
    usage = monitor.sample("run-1", _sample(cpu), now=now)  # 0% window.
    assert usage["high_duration_s"] == 0.0
    assert usage["abnormal"] is False
    for _ in range(35):
        now += 5.0
        cpu += 3.0
        usage = monitor.sample("run-1", _sample(cpu), now=now)
    assert usage["high_duration_s"] == 175.0
    assert usage["abnormal"] is False


def test_gap_invalid_reading_and_identity_change_do_not_count_blank_time():
    monitor = _monitor()
    monitor.register("run-1", 123, 99)
    monitor.sample("run-1", _sample(0), now=0)
    usage = monitor.sample("run-1", _sample(25), now=25)
    assert usage["monitoring_status"] == "unknown"
    assert usage["cpu_percent"] is None
    assert usage["high_duration_s"] == 0.0

    usage = monitor.sample("run-1", _sample(28), now=30)
    assert usage["high_duration_s"] == 5.0
    usage = monitor.sample("run-1", None, now=35)
    assert usage["monitoring_status"] == "unknown"
    assert usage["cpu_percent"] is None
    assert usage["high_duration_s"] == 0.0

    usage = monitor.sample(
        "run-1", ProcCpuSample(pid=123, start_time_ticks=100, cpu_ticks=4000), now=40
    )
    assert usage["monitoring_status"] == "unknown"
    assert usage["abnormal"] is False


def test_alert_stays_locked_after_cpu_drops_and_stop_keeps_it():
    monitor = _monitor()
    monitor.register("run-1", 123, 99)
    monitor.sample("run-1", _sample(0), now=0)
    now = 0.0
    cpu = 0.0
    for _ in range(36):
        now += 5.0
        cpu += 3.0
        usage = monitor.sample("run-1", _sample(cpu), now=now)
    assert usage["abnormal"] is True

    now += 5.0
    usage = monitor.sample("run-1", _sample(cpu), now=now)
    assert usage["abnormal"] is True
    assert usage["cpu_percent"] == 0.0
    stopped = monitor.mark_stopped("run-1")
    assert stopped["monitoring_status"] == "stopped"
    assert stopped["cpu_percent"] is None
    assert stopped["abnormal"] is True


def test_new_runtime_starts_clean_and_clear_removes_state():
    monitor = _monitor()
    monitor.register("run-1", 123, 99)
    state = monitor._states["run-1"]
    state.abnormal = True
    state.detected_at = "2026-11-14T00:00:00+00:00"
    monitor.register("run-2", 456, 100)
    usage = monitor.snapshot("run-2")
    assert usage["runtime_id"] == "run-2"
    assert usage["abnormal"] is False
    assert usage["required_duration_s"] == 180
    monitor.clear("run-2")
    assert monitor.snapshot("run-2")["monitoring_status"] == "unknown"
