"""Per-source Linux CPU sampling and sustained-load alert state.

The monitor intentionally measures one source worker process, not the shared
lxserver or the whole container. CPU is expressed against one logical core:
``100 * delta_cpu_seconds / delta_monotonic_seconds``. Values above 100% are
kept as-is; no division by host core count or container CPU quota is applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import time
from typing import Callable


CPU_BASIS = "single_core"
DEFAULT_THRESHOLD_PERCENT = 50.0
DEFAULT_REQUIRED_DURATION_S = 180.0
DEFAULT_SAMPLE_INTERVAL_S = 5.0
DEFAULT_MAX_GAP_S = 15.0


@dataclass(frozen=True)
class ProcCpuSample:
    """A validated kernel accounting sample for one process identity."""

    pid: int
    start_time_ticks: int
    cpu_ticks: int


@dataclass
class _RuntimeCpuState:
    runtime_id: str
    pid: int
    start_time_ticks: int
    baseline_monotonic: float | None = None
    baseline_cpu_seconds: float | None = None
    high_duration_s: float = 0.0
    abnormal: bool = False
    detected_at: str = ""
    cpu_percent: float | None = None
    monitoring_status: str = "unknown"


def linux_monitoring_available(proc_root: str | Path = "/proc") -> bool:
    """Monitoring is only trustworthy on Linux with a readable procfs."""

    return os.name == "posix" and Path(proc_root).is_dir()


def clock_ticks_per_second() -> int:
    try:
        value = int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        value = 100
    return value if value > 0 else 100


def parse_proc_stat(text: str, *, clock_ticks: int | None = None) -> ProcCpuSample:
    """Parse ``/proc/<pid>/stat`` while tolerating spaces/parentheses in comm."""

    raw = str(text or "")
    left = raw.find("(")
    right = raw.rfind(")")
    if left <= 0 or right <= left:
        raise ValueError("invalid proc stat")
    try:
        pid = int(raw[:left].strip())
    except ValueError as exc:
        raise ValueError("invalid proc pid") from exc
    fields = raw[right + 1 :].split()
    if len(fields) < 20:
        raise ValueError("short proc stat")
    try:
        user_ticks = int(fields[11])
        system_ticks = int(fields[12])
        start_time_ticks = int(fields[19])
    except (IndexError, ValueError) as exc:
        raise ValueError("invalid proc accounting") from exc
    # Keep the field calculation explicit for callers reading the contract.
    _ = clock_ticks if clock_ticks is not None else clock_ticks_per_second()
    return ProcCpuSample(
        pid=pid,
        start_time_ticks=start_time_ticks,
        cpu_ticks=max(0, user_ticks) + max(0, system_ticks),
    )


def read_proc_stat(
    pid: int,
    *,
    proc_root: str | Path = "/proc",
    clock_ticks: int | None = None,
) -> ProcCpuSample | None:
    """Read one process sample; return None for missing/invalid/unreadable data."""

    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        text = (Path(proc_root) / str(pid) / "stat").read_text(encoding="utf-8")
        return parse_proc_stat(text, clock_ticks=clock_ticks)
    except (OSError, UnicodeError, ValueError):
        return None


class SourceCpuMonitor:
    """Track CPU windows per runtime and lock the alert for that runtime."""

    def __init__(
        self,
        *,
        threshold_percent: float = DEFAULT_THRESHOLD_PERCENT,
        required_duration_s: float = DEFAULT_REQUIRED_DURATION_S,
        sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
        max_gap_s: float = DEFAULT_MAX_GAP_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        clock_ticks: int | None = None,
        proc_root: str | Path = "/proc",
    ):
        self.threshold_percent = float(threshold_percent)
        self.required_duration_s = float(required_duration_s)
        self.sample_interval_s = float(sample_interval_s)
        self.max_gap_s = float(max_gap_s)
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._clock_ticks = int(clock_ticks or clock_ticks_per_second())
        self._proc_root = Path(proc_root)
        self._states: dict[str, _RuntimeCpuState] = {}

    @property
    def supported(self) -> bool:
        return linux_monitoring_available(self._proc_root)

    def register(self, runtime_id: str, pid: int, start_time_ticks: int) -> None:
        """Start a fresh baseline for a new process runtime."""

        rid = str(runtime_id or "")
        if not rid:
            raise ValueError("runtime_id is required")
        prior = self._states.get(rid)
        if (
            prior is not None
            and prior.pid == int(pid)
            and prior.start_time_ticks == int(start_time_ticks)
        ):
            return
        self._states[rid] = _RuntimeCpuState(
            runtime_id=rid,
            pid=int(pid),
            start_time_ticks=int(start_time_ticks),
        )

    def clear(self, runtime_id: str) -> None:
        self._states.pop(str(runtime_id or ""), None)

    def sample(
        self,
        runtime_id: str,
        sample: ProcCpuSample | None,
        *,
        now: float | None = None,
    ) -> dict:
        """Apply one kernel sample and return the public resource_usage object."""

        rid = str(runtime_id or "")
        state = self._states.get(rid)
        if state is None:
            return self._payload(None, rid, monitoring_status="unknown")

        current = self._clock() if now is None else float(now)
        if sample is None or not self._same_identity(state, sample):
            self._reset_window(state)
            state.cpu_percent = None
            state.monitoring_status = "unknown"
            return self._payload(state, rid)

        cpu_seconds = float(sample.cpu_ticks) / float(self._clock_ticks)
        if state.baseline_monotonic is None or state.baseline_cpu_seconds is None:
            self._set_baseline(state, current, cpu_seconds)
            state.cpu_percent = None
            state.monitoring_status = "ok"
            return self._payload(state, rid)

        elapsed = current - state.baseline_monotonic
        if elapsed <= 0:
            self._set_baseline(state, current, cpu_seconds)
            state.cpu_percent = None
            state.monitoring_status = "unknown"
            return self._payload(state, rid)
        if elapsed > self.max_gap_s:
            # A long absence cannot be counted as sustained CPU usage.
            self._set_baseline(state, current, cpu_seconds)
            if not state.abnormal:
                state.high_duration_s = 0.0
            state.cpu_percent = None
            state.monitoring_status = "unknown"
            return self._payload(state, rid)
        delta_cpu = cpu_seconds - state.baseline_cpu_seconds
        if delta_cpu < 0:
            self._set_baseline(state, current, cpu_seconds)
            if not state.abnormal:
                state.high_duration_s = 0.0
            state.cpu_percent = None
            state.monitoring_status = "unknown"
            return self._payload(state, rid)

        percent = 100.0 * delta_cpu / elapsed
        state.cpu_percent = percent
        state.monitoring_status = "ok"
        if percent > self.threshold_percent:
            state.high_duration_s += elapsed
            if not state.abnormal and state.high_duration_s >= self.required_duration_s:
                state.abnormal = True
                state.detected_at = self._wall_clock().astimezone(timezone.utc).isoformat()
        elif not state.abnormal:
            state.high_duration_s = 0.0
        self._set_baseline(state, current, cpu_seconds)
        return self._payload(state, rid)

    def mark_stopped(self, runtime_id: str) -> dict:
        rid = str(runtime_id or "")
        state = self._states.get(rid)
        if state is None:
            return self._payload(None, rid, monitoring_status="stopped")
        state.cpu_percent = None
        state.monitoring_status = "stopped"
        return self._payload(state, rid)

    def snapshot(self, runtime_id: str) -> dict:
        rid = str(runtime_id or "")
        return self._payload(self._states.get(rid), rid)

    def read_sample(self, runtime_id: str) -> ProcCpuSample | None:
        state = self._states.get(str(runtime_id or ""))
        if state is None:
            return None
        return read_proc_stat(
            state.pid,
            proc_root=self._proc_root,
            clock_ticks=self._clock_ticks,
        )

    def matches_process(self, runtime_id: str, pid: int, start_time_ticks: int) -> bool:
        state = self._states.get(str(runtime_id or ""))
        return bool(
            state is not None
            and state.pid == int(pid)
            and state.start_time_ticks == int(start_time_ticks)
        )

    @staticmethod
    def _same_identity(state: _RuntimeCpuState, sample: ProcCpuSample) -> bool:
        return (
            sample.pid == state.pid
            and sample.start_time_ticks == state.start_time_ticks
        )

    @staticmethod
    def _set_baseline(state: _RuntimeCpuState, now: float, cpu_seconds: float) -> None:
        state.baseline_monotonic = float(now)
        state.baseline_cpu_seconds = float(cpu_seconds)

    @staticmethod
    def _reset_window(state: _RuntimeCpuState) -> None:
        state.baseline_monotonic = None
        state.baseline_cpu_seconds = None
        if not state.abnormal:
            state.high_duration_s = 0.0

    def _payload(
        self,
        state: _RuntimeCpuState | None,
        runtime_id: str,
        *,
        monitoring_status: str | None = None,
    ) -> dict:
        status = monitoring_status or (
            state.monitoring_status if state is not None else "unknown"
        )
        if state is None:
            return {
                "monitoring_status": status,
                "cpu_basis": CPU_BASIS,
                "cpu_percent": None,
                "threshold_percent": self.threshold_percent,
                "required_duration_s": int(self.required_duration_s)
                if self.required_duration_s.is_integer() else self.required_duration_s,
                "high_duration_s": 0.0,
                "abnormal": False,
                "reason": "",
                "detected_at": "",
                "runtime_id": str(runtime_id or ""),
            }
        return {
            "monitoring_status": status,
            "cpu_basis": CPU_BASIS,
            "cpu_percent": (
                None if state.cpu_percent is None else round(state.cpu_percent, 1)
            ),
            "threshold_percent": self.threshold_percent,
            "required_duration_s": int(self.required_duration_s)
            if self.required_duration_s.is_integer() else self.required_duration_s,
            "high_duration_s": round(state.high_duration_s, 1),
            "abnormal": bool(state.abnormal),
            "reason": "sustained_source_cpu" if state.abnormal else "",
            "detected_at": state.detected_at if state.abnormal else "",
            "runtime_id": state.runtime_id,
        }
