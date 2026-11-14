#!/usr/bin/env python3
"""Real per-source CPU alert acceptance runner.

This script starts one deliberately CPU-heavy synthetic LX source and one normal
control source in isolated Node workers, then polls the public source snapshot.
It is intended for an isolated Linux environment with at least one full CPU
core available to the test container.

The default run follows the acceptance contract: 5-second samples, >50% of one
logical core, and 180 seconds of continuous high usage. For a fast local smoke
test, pass smaller ``--duration-s`` / ``--required-duration-s`` values.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
LXSERVICE = REPO_ROOT / "lxmusic-service"
if str(LXSERVICE) not in sys.path:
    sys.path.insert(0, str(LXSERVICE))

import source_cpu as sc  # noqa: E402
import source_runtime as sr  # noqa: E402


HIGH_CPU_SOURCE = """/*!
 * @name CPU Acceptance High
 * @version 1.0.0
 */
const busyUntil = Date.now() + __BUSY_MS__;
const timer = setInterval(() => {
  if (Date.now() >= busyUntil) return;
  while (Date.now() < busyUntil && Date.now() < busyUntil - 1) {
    // Deliberately occupy the worker event loop in short slices.
  }
}, 1);
setTimeout(() => clearInterval(timer), __BUSY_MS__ + 50);
globalThis.lx.on('request', async () => 'https://media.test/high.flac');
globalThis.lx.send('inited', {sources: {kw: {qualitys: ['128k']}}});
"""

NORMAL_CPU_SOURCE = """/*!
 * @name CPU Acceptance Control
 * @version 1.0.0
 */
globalThis.lx.on('request', async () => 'https://media.test/normal.flac');
globalThis.lx.send('inited', {sources: {kw: {qualitys: ['128k']}}});
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LX per-source CPU alert acceptance")
    parser.add_argument(
        "--lxserver-dir",
        default=os.environ.get("FNMUSIC_TEST_LXSERVER_DIR")
        or os.environ.get("LXSERVER_DIR")
        or "/srv/lxserver",
        help="lxserver v2.2.0 tree containing server/server/sources/userApi.js",
    )
    parser.add_argument("--duration-s", type=float, default=190.0,
                        help="how long the synthetic source keeps the CPU busy")
    parser.add_argument("--threshold-percent", type=float, default=50.0)
    parser.add_argument("--required-duration-s", type=float, default=180.0)
    parser.add_argument("--sample-interval-s", type=float, default=5.0)
    parser.add_argument("--temp-root", default="",
                        help="optional writable parent directory for the isolated source tree")
    parser.add_argument("--timeout-s", type=float, default=0.0,
                        help="overall safety deadline; defaults to duration + 120s")
    return parser


def _validate(args: argparse.Namespace) -> tuple[Path, Path]:
    lxserver = Path(args.lxserver_dir).expanduser().resolve()
    user_api = lxserver / "server" / "server" / "sources" / "userApi.js"
    if not user_api.is_file():
        raise SystemExit(f"lxserver v2.2.0 module not found: {user_api}")
    if not sc.linux_monitoring_available():
        raise SystemExit("per-source CPU monitoring requires Linux procfs")
    if shutil.which("node") is None:
        raise SystemExit("node is not available")
    if args.sample_interval_s <= 0 or args.sample_interval_s > sc.DEFAULT_MAX_GAP_S:
        raise SystemExit("--sample-interval-s must be > 0 and <= 15")
    if args.required_duration_s <= 0 or args.duration_s <= 0:
        raise SystemExit("durations must be positive")
    if args.duration_s < args.required_duration_s + args.sample_interval_s * 2:
        raise SystemExit("--duration-s must exceed required duration by at least two sample windows")
    if args.threshold_percent <= 0 or args.threshold_percent > 100:
        raise SystemExit("--threshold-percent must be in (0, 100]")
    temp_root = Path(args.temp_root).expanduser() if args.temp_root else None
    if temp_root is not None:
        temp_root.mkdir(parents=True, exist_ok=True)
    return lxserver, temp_root


def _row(rows: list[dict], source_id: str) -> dict:
    for row in rows:
        if str(row.get("id") or "") == source_id:
            return row
    raise RuntimeError(f"source row disappeared: {source_id}")


async def _run(args: argparse.Namespace, lxserver: Path, temp_parent: Path | None) -> dict:
    started = time.monotonic()
    timeout_s = args.timeout_s or (args.duration_s + 120.0)
    temp_dir = tempfile.mkdtemp(prefix="fnmusic-lx-cpu-", dir=str(temp_parent) if temp_parent else None)
    root = Path(temp_dir)
    manager = sr.SourceRuntimeManager(
        source_root=root / "sources",
        lxserver_dir=lxserver,
        node_bin=shutil.which("node") or "node",
        init_timeout_s=20,
        stop_budget_s=5,
        monitor_interval_s=args.sample_interval_s,
        cpu_monitor=sc.SourceCpuMonitor(
            threshold_percent=args.threshold_percent,
            required_duration_s=args.required_duration_s,
            sample_interval_s=args.sample_interval_s,
            max_gap_s=max(sc.DEFAULT_MAX_GAP_S, args.sample_interval_s * 3),
        ),
    )
    summary: dict = {
        "threshold_percent": args.threshold_percent,
        "required_duration_s": args.required_duration_s,
        "sample_interval_s": args.sample_interval_s,
        "duration_s": args.duration_s,
        "checks": {},
        "samples": [],
    }
    pids: list[int] = []
    high_id = ""
    normal_id = ""
    try:
        await manager.start()
        high_script = HIGH_CPU_SOURCE.replace(
            "__BUSY_MS__", str(int(args.duration_s * 1000))
        )
        high = await manager.upload_source(
            "cpu-high.js", high_script, deadline=time.monotonic() + 25
        )
        normal = await manager.upload_source(
            "cpu-normal.js", NORMAL_CPU_SOURCE, deadline=time.monotonic() + 25
        )
        high_id, normal_id = high["id"], normal["id"]
        await manager.set_enabled(high_id, True, deadline=time.monotonic() + 25)
        await manager.set_enabled(normal_id, True, deadline=time.monotonic() + 25)
        rows = await manager.list_sources()
        high_row = _row(rows, high_id)
        normal_row = _row(rows, normal_id)
        pids = [int(manager._runtimes[high_id].pid), int(manager._runtimes[normal_id].pid)]
        summary["runtime_ids"] = {
            "high": high_row["runtime_id"],
            "normal": normal_row["runtime_id"],
        }
        summary["checks"]["two_distinct_processes"] = (
            len(set(pids)) == 2 and high_row["runtime_id"] != normal_row["runtime_id"]
        )
        summary["checks"]["initial_state_clean"] = (
            high_row["resource_usage"]["abnormal"] is False
            and normal_row["resource_usage"]["abnormal"] is False
        )

        deadline = started + timeout_s
        high_alert_at: float | None = None
        normal_resolved_while_high = False
        last_resolve = 0.0
        while time.monotonic() < deadline:
            rows = await manager.list_sources()
            high_row = _row(rows, high_id)
            normal_row = _row(rows, normal_id)
            high_usage = high_row["resource_usage"]
            normal_usage = normal_row["resource_usage"]
            summary["samples"].append({
                "t_s": round(time.monotonic() - started, 2),
                "high_cpu": high_usage.get("cpu_percent"),
                "high_high_s": high_usage.get("high_duration_s"),
                "high_abnormal": high_usage.get("abnormal"),
                "normal_cpu": normal_usage.get("cpu_percent"),
                "normal_abnormal": normal_usage.get("abnormal"),
            })
            if high_usage.get("abnormal") is True and high_alert_at is None:
                high_alert_at = time.monotonic()
                summary["checks"]["high_alert_locked"] = True
                summary["checks"]["normal_not_flagged"] = normal_usage.get("abnormal") is False
                summary["high_alert_elapsed_s"] = round(high_alert_at - started, 2)
                summary["high_alert_readings"] = {
                    "cpu_percent": high_usage.get("cpu_percent"),
                    "high_duration_s": high_usage.get("high_duration_s"),
                    "detected_at": high_usage.get("detected_at"),
                    "monitoring_status": high_usage.get("monitoring_status"),
                }
                break
            if time.monotonic() - last_resolve >= max(1.0, args.sample_interval_s):
                result = await manager.resolve(
                    normal_id, {"source": "kw"}, "128k",
                    deadline=time.monotonic() + 5,
                )
                normal_resolved_while_high = result["url"] == "https://media.test/normal.flac"
                last_resolve = time.monotonic()
            if normal_usage.get("abnormal") is True:
                raise RuntimeError("control source was incorrectly flagged")
            await asyncio.sleep(min(args.sample_interval_s, max(0.05, deadline - time.monotonic())))

        if high_alert_at is None:
            summary["checks"]["high_alert_locked"] = False
            raise RuntimeError("high-CPU source did not trigger the alert before the deadline")
        summary["checks"]["normal_resolved_while_high"] = normal_resolved_while_high

        # Let the synthetic busy window expire, then confirm the alert is locked
        # even after a fresh low-CPU window replaces the high reading.
        busy_end = started + args.duration_s + args.sample_interval_s * 2
        quiet_deadline = min(deadline, max(busy_end, time.monotonic()) + args.sample_interval_s * 3)
        while time.monotonic() < quiet_deadline:
            rows = await manager.list_sources()
            high_row = _row(rows, high_id)
            usage = high_row["resource_usage"]
            if (
                time.monotonic() >= busy_end
                and usage.get("monitoring_status") == "ok"
                and (usage.get("cpu_percent") or 0) <= args.threshold_percent
            ):
                break
            await asyncio.sleep(min(args.sample_interval_s, max(0.05, quiet_deadline - time.monotonic())))
        rows = await manager.list_sources()
        high_row = _row(rows, high_id)
        summary["checks"]["alert_retained_after_low_cpu"] = (
            high_row["resource_usage"].get("abnormal") is True
            and high_row["resource_usage"].get("monitoring_status") == "ok"
            and (high_row["resource_usage"].get("cpu_percent") or 0) <= args.threshold_percent
        )
        summary["post_alert_reading"] = {
            "cpu_percent": high_row["resource_usage"].get("cpu_percent"),
            "abnormal": high_row["resource_usage"].get("abnormal"),
        }

        old_runtime = high_row["runtime_id"]
        stopped = await manager.set_enabled(high_id, False, deadline=time.monotonic() + 10)
        summary["checks"]["disable_confirmed"] = stopped is True
        rows = await manager.list_sources()
        high_row = _row(rows, high_id)
        summary["checks"]["alert_cleared_on_disable"] = (
            high_row["resource_usage"].get("abnormal") is False
            and high_row["resource_usage"].get("monitoring_status") == "stopped"
            and high_row["runtime_id"] == ""
        )

        await manager.set_enabled(high_id, True, deadline=time.monotonic() + 25)
        rows = await manager.list_sources()
        high_row = _row(rows, high_id)
        summary["checks"]["new_runtime_after_restart"] = (
            bool(high_row["runtime_id"]) and high_row["runtime_id"] != old_runtime
        )
        summary["checks"]["alert_reset_on_restart"] = (
            high_row["resource_usage"].get("abnormal") is False
        )
        await manager.set_enabled(high_id, False, deadline=time.monotonic() + 10)

        summary["checks"]["control_source_still_healthy"] = (
            _row(await manager.list_sources(), normal_id)["status"] == "success"
        )
        summary["ok"] = all(bool(value) for value in summary["checks"].values())
        return summary
    finally:
        try:
            await manager.close()
        finally:
            await asyncio.sleep(0.2)
            summary["processes_cleaned"] = all(
                not Path(f"/proc/{pid}").exists() for pid in pids
            )
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    args = _parser().parse_args()
    lxserver, temp_parent = _validate(args)
    try:
        summary = asyncio.run(_run(args, lxserver, temp_parent))
    except Exception as exc:  # noqa: BLE001 - CLI must emit one bounded failure
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc)[:240],
        }, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") and summary.get("processes_cleaned") else 1


if __name__ == "__main__":
    raise SystemExit(main())
