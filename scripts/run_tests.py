#!/usr/bin/env python3
"""Run the offline suites without sharing app/conftest/environment globals.

Usage: python3 scripts/run_tests.py [--jobs 2] [--shards 2] [--suite webui]

Each configured root in pytest.ini is scheduled exactly once. proxy tests are
split into balanced file shards running in separate interpreters (never threads,
never xdist); files that bind host resources or mutate process-global state stay
in one exclusive shard. Every shard gets its own app home, registries, caches and
pytest basetemp, so two shards never write the same file.
Node behavior tests are already included by webui-service/test_app_js.py.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[1]
# Longest services first. Each configured pytest testpath appears exactly once.
PHASES = (
    {"proxy": ("proxy/tests",)},
    {
        "webui": ("webui-service",),
        "musicdl": ("musicdl-service",),
        "lxmusic": ("lxmusic-service",),
    },
    # 只含离线pytest用例；真实第三方脚本仍须单独显式授权矩阵CLI。
    {"infrastructure": ("container", "packaging/tests", "tests/runner", "tests/integration")},
)
SUITES = {name: paths for phase in PHASES for name, paths in phase.items()}
COVERAGE_TARGETS = (
    "proxy", "lxmusic-service", "musicdl-service", "musicbox-service", "webui-service",
)
PROXY_ROOT = "proxy/tests"
# Files whose tests bind host-level resources (shell scripts, sockets, nginx,
# real subprocess services) or mutate process-global state. One serial shard.
PROXY_EXCLUSIVE = (
    "test_build_fingerprint.py",
    "test_fnlog.py",
    "test_image_bundle.py",
    "test_install_edges.py",
    "test_installation_no_download.py",
    "test_installation_reliability.py",
    "test_log_retention.py",
    "test_maintenance.py",
    "test_maintenance_jobs.py",
    "test_maintenance_socket.py",
    "test_musicbox_service.py",
    "test_musicdl_distribution.py",
    "test_native_deploy.py",
    "test_netease_sky_quality.py",
    "test_organize_review.py",
    "test_reverse_proxy.py",
    "test_transcode_compat.py",
    "test_version_env.py",
    "test_webui_gateway.py",
)
# Cost hints for balancing only; correctness never depends on them. Unknown or
# new files get DEFAULT_FILE_WEIGHT and still land in exactly one shard.
PROXY_FILE_WEIGHTS = {
    "test_public_playlists.py": 6.0,
    "test_issue75_lx_playback.py": 5.0,
    "test_lxmusic.py": 4.0,
    "test_source_tag.py": 4.0,
    "test_proxy_reliability.py": 3.5,
    "test_recommend.py": 3.0,
    "test_local_cover.py": 3.0,
    "test_merge.py": 3.0,
    "test_tee_cache.py": 3.0,
    "test_official_bind.py": 2.5,
    "test_netease.py": 2.5,
    "test_direct_play_https.py": 2.0,
    "test_fav_rolling_promote.py": 2.0,
    "test_nm_playlists.py": 2.0,
    "test_playlist_tracks.py": 2.0,
    "test_v2_features.py": 1.5,
    "test_direct_play.py": 1.5,
}
DEFAULT_FILE_WEIGHT = 1.0


@dataclass
class Running:
    process: subprocess.Popen
    log: BinaryIO
    started: float


def pytest_command(paths: tuple[str, ...], coverage: bool) -> list[str]:
    command = [sys.executable, "-m", "pytest", *paths, "-q", "--durations=5"]
    if coverage:
        command.extend(f"--cov={target}" for target in COVERAGE_TARGETS)
        command.append("--cov-report=")
    return command


def stop_children(running: dict[str, Running]) -> None:
    """Reap interrupted suites, including their socket/worker subprocesses."""
    for item in running.values():
        if os.name == "posix":
            try:
                os.killpg(item.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif item.process.poll() is None:
            item.process.terminate()
    deadline = time.monotonic() + 5
    for item in running.values():
        try:
            item.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # The pytest leader may exit before one of its workers. Kill the whole group,
    # not just leaders whose poll() is still None.
    for item in running.values():
        if os.name == "posix":
            try:
                os.killpg(item.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif item.process.poll() is None:
            item.process.kill()
        item.process.wait()
        item.log.close()


def proxy_test_files() -> list[str]:
    """Discover proxy test files dynamically so a new file is never dropped."""
    root = ROOT / PROXY_ROOT
    return sorted(p.name for p in root.glob("test_*.py") if p.is_file())


def proxy_shards(count: int) -> list[tuple[str, ...]]:
    """Split proxy files into balanced, disjoint shards, then one exclusive shard.

    Every discovered file lands in exactly one shard: new files are scheduled by
    default instead of silently skipped, and no file is scheduled twice.
    """
    files = proxy_test_files()
    if not files:
        return []
    if count <= 1:
        return [(PROXY_ROOT,)]
    exclusive = [f for f in files if f in PROXY_EXCLUSIVE]
    shared = [f for f in files if f not in PROXY_EXCLUSIVE]
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for name in sorted(shared, key=lambda n: (-PROXY_FILE_WEIGHTS.get(n, DEFAULT_FILE_WEIGHT), n)):
        index = loads.index(min(loads))
        shards[index].append(name)
        loads[index] += PROXY_FILE_WEIGHTS.get(name, DEFAULT_FILE_WEIGHT)
    plan = [tuple(f"{PROXY_ROOT}/{name}" for name in shard) for shard in shards if shard]
    if exclusive:
        plan.append(tuple(f"{PROXY_ROOT}/{name}" for name in exclusive))
    return plan


def run_phase(
    suites: dict[str, tuple[str, ...]], jobs: int, work: Path, coverage: bool,
) -> dict[str, int]:
    pending = iter(suites.items())
    running: dict[str, Running] = {}
    results: dict[str, int] = {}
    exhausted = False
    try:
        while running or not exhausted:
            while not exhausted and len(running) < jobs:
                entry = next(pending, None)
                if entry is None:
                    exhausted = True
                    break
                name, paths = entry
                env = os.environ.copy()
                # Each process owns its cache and coverage output. tmp_path uses
                # pytest's unique per-session base (short enough for Unix sockets).
                env["COVERAGE_FILE"] = str(work / "coverage" / f".coverage.{name}")
                # Shards share one checkout, so give each interpreter its own app
                # home, registry and caches; no two processes write the same file.
                isolated = work / name
                for sub in ("home", "logs", "cache", "library", "fav", "tmp"):
                    (isolated / sub).mkdir(parents=True, exist_ok=True)
                env.update({
                    "FNMUSIC_HOME": str(isolated / "home"),
                    "FNMUSIC_LOG_DIR": str(isolated / "logs"),
                    "FNMUSIC_ALBUM_REGISTRY": str(isolated / "home" / "album_registry.json"),
                    "FNMUSIC_CACHE_DIR": str(isolated / "cache"),
                    "FNMUSIC_LIBRARY_DIR": str(isolated / "library"),
                    "FNMUSIC_FAV_DIR": str(isolated / "fav"),
                })
                command = pytest_command(paths, coverage)
                command.extend([
                    "-o", f"cache_dir={isolated / 'cache' / 'pytest'}",
                    f"--basetemp={isolated / 'tmp'}",
                ])
                log = (work / f"{name}.log").open("w+b")
                print(f"=== {name}: starting ===", flush=True)
                try:
                    process = subprocess.Popen(
                        command, cwd=ROOT, env=env, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=(os.name == "posix"),
                    )
                except BaseException:
                    log.close()
                    raise
                running[name] = Running(process, log, time.monotonic())
            for name, item in list(running.items()):
                code = item.process.poll()
                if code is None:
                    continue
                elapsed = time.monotonic() - item.started
                item.log.seek(0)
                print(item.log.read().decode("utf-8", errors="replace"), end="", flush=True)
                item.log.close()
                print(f"=== {name}: exit {code}, {elapsed:.2f}s ===", flush=True)
                results[name] = code
                del running[name]
            if running:
                time.sleep(0.05)
    finally:
        stop_children(running)
    return results


def coverage_report(work: Path) -> int:
    env = os.environ.copy()
    env["COVERAGE_FILE"] = str(work / ".coverage")
    combined = subprocess.run(
        [sys.executable, "-m", "coverage", "combine", str(work / "coverage")],
        cwd=ROOT, env=env,
    )
    if combined.returncode:
        return combined.returncode
    return subprocess.run(
        [sys.executable, "-m", "coverage", "report"], cwd=ROOT, env=env,
    ).returncode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, choices=(1, 2, 3), default=2,
                        help="parallel interpreters (default: 2; 1 disables overlap)")
    parser.add_argument("--suite", action="append", choices=tuple(SUITES),
                        help="select suites; repeat to select more (default: all)")
    parser.add_argument("--coverage", action="store_true",
                        help="combine per-process coverage; requires pytest-cov")
    parser.add_argument("--shards", type=int, choices=(1, 2, 3), default=2,
                        help="proxy file shards (default: 2; 1 runs proxy in one pass)")
    return parser.parse_args(argv)


def interrupt(signum: int, frame: object) -> None:
    # CI cancellation sends TERM; unwind run_phase so process groups are reaped.
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = set(args.suite or SUITES)
    started = time.monotonic()
    results: dict[str, int] = {}
    shards = proxy_shards(args.shards)
    try:
        with tempfile.TemporaryDirectory(prefix="fn-tests-") as directory:
            work = Path(directory)
            (work / "coverage").mkdir()
            for index, phase in enumerate(PHASES):
                suites = {name: paths for name, paths in phase.items() if name in selected}
                if index == 0 and "proxy" in suites:
                    # Separate interpreters, never threads or xdist.
                    suites = {
                        f"proxy-{number + 1}": paths
                        for number, paths in enumerate(shards)
                    }
                results.update(run_phase(suites, args.jobs, work, args.coverage))
            report_code = coverage_report(work) if args.coverage else 0
    except KeyboardInterrupt:
        print("\nInterrupted; test process groups stopped.", file=sys.stderr)
        return 130
    failed = [name for name, code in results.items() if code]
    print(f"=== Total: {len(results)} suites, {len(failed)} failed, "
          f"{time.monotonic() - started:.2f}s ===", flush=True)
    if failed:
        print("Failed suites: " + ", ".join(failed), file=sys.stderr)
    return int(bool(failed or report_code))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
