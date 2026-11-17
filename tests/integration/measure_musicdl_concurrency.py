#!/usr/bin/env python3
"""诊断工具：并发度对 musicdl 单源 worker 启动/搜索时延的影响（CT201 实机）。

不是 pass/fail 测试；用于评估调整 MUSICDL_ADAPTIVE_MIN_TIMEOUT / 源白名单前的基线。
用法:
  python3 tests/integration/measure_musicdl_concurrency.py \
      --sources KuwoMusicClient,MiguMusicClient --budget 12 --keyword 周杰伦

注意：worker.py 必须与 musicdl-service 同目录运行（脚本会 cd 到 /repo/musicdl-service）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def run_one(source, keyword, fetch_size, limit, budget, workdir):
    task = {"op": "search", "source": source, "keyword": keyword,
            "fetch_size": fetch_size, "limit": limit, "song_id": None,
            "budget_s": budget}
    t0 = time.time()
    p = subprocess.Popen(
        ["python3", "worker.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=workdir)
    try:
        p.stdout.readline()  # ready
        startup = time.time() - t0
        p.stdin.write(json.dumps(task, ensure_ascii=False) + "\n")
        p.stdin.flush()
        first = None
        while True:
            line = p.stdout.readline()
            if not line:
                break
            m = json.loads(line)
            if m["type"] == "entry" and first is None:
                first = time.time() - t0
            if m["type"] in ("result", "error"):
                return (source, round(startup, 2),
                        None if first is None else round(first, 2),
                        round(time.time() - t0, 2), m.get("count"),
                        m.get("error") or ("partial" if m.get("partial") else "ok"))
    finally:
        p.kill()
    return (source, round(startup, 2), None, round(time.time() - t0, 2), 0, "no-result")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="KuwoMusicClient,MiguMusicClient")
    ap.add_argument("--budget", type=float, default=12.0)
    ap.add_argument("--keyword", default="周杰伦")
    ap.add_argument("--fetch-size", type=int, default=20)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--workdir", default="/repo/musicdl-service")
    args = ap.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        for r in ex.map(lambda s: run_one(s, args.keyword, args.fetch_size,
                                          args.limit, args.budget, args.workdir), sources):
            print(r, flush=True)
    print("wall", round(time.time() - t0, 2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
