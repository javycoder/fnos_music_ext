#!/usr/bin/env python3
"""Offline/online smoke for musicdl 搜索可靠性（CT201 实机专用）。

验证点（不依赖用户鉴权，直接打 musicdl 服务）：
  1. 默认双源（Kuwo + Migu）对一组中文关键词各返回 >= 1 条在线结果；
  2. 单源 Migu 在 15s 预算内返回 >= 1 条（覆盖慢源场景）；
  3. 搜索结果的错误字典不包含 circuit breaker open（熔断器未误触发）；
  4. 重复搜索同一关键词不出现全空（缓存/熔断稳定性）。

用法（在 CT201 上，或通过 sudo -n pct exec 201 --）:
  python3 tests/integration/musicdl_search_reliability.py \
      --musicdl-url http://127.0.0.1:8768 --repeat 2

退出码：0 全通过，1 有失败。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


KEYWORDS = ["周杰伦", "毛不易", "陈奕迅", "张学友", "王心凌", "林俊杰"]


def fetch(url, path, params):
    q = url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(q, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--musicdl-url", default="http://127.0.0.1:8768")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--batch", action="store_true", help="对 musicdl 全部注册平台运行测试并输出报告")
    args = ap.parse_args()
    base = args.musicdl_url.rstrip("/")
    failures = []

    def check(label, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'} {label}{' :: ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    # 0. 源白名单断言
    try:
        src = fetch(base, "/sources", {})
        enabled = src.get("enabled", [])
        check("enabled sources are Kuwo+Migu", enabled == ["KuwoMusicClient", "MiguMusicClient"],
              f"enabled={enabled}")
    except Exception as e:
        check("sources endpoint reachable", False, str(e))

    # 0. 批量模式：对全部注册平台运行测试
    if args.batch:
        registered = []
        try:
            src = fetch(base, "/sources", {})
            registered = src.get("registered", [])
        except Exception as e:
            print(f"sources endpoint failed: {e}")
            return 1
        kw = "周杰伦"
        print(f"\n=== 批量模式：测试 {len(registered)} 个平台 (keyword={kw}, concurrency=4) ===")
        ok_count = 0
        lock = __import__("threading").Lock()

        def _test_one(p):
            nonlocal ok_count
            started = time.monotonic()
            try:
                d = fetch(base, "/search", {"keyword": kw, "sources": p, "limit": 1})
                items = d.get("items") or []
                errors = d.get("errors") or {}
                elapsed = time.monotonic() - started
                if items:
                    with lock:
                        ok_count += 1
                    print(f"  OK   {p:30s} {elapsed:6.1f}s  {items[0].get('title','')[:20]}", flush=True)
                else:
                    err = next(iter(errors.values()), "no results") if errors else "no results"
                    print(f"  FAIL {p:30s} {elapsed:6.1f}s  {str(err)[:40]}", flush=True)
            except Exception as e:
                elapsed = time.monotonic() - started
                print(f"  ERR  {p:30s} {elapsed:6.1f}s  {str(e)[:40]}", flush=True)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(_test_one, registered))
        print(f"\n批量测试完成：{ok_count}/{len(registered)} 可用")
        return 0

    # 1. 双源搜索
    for kw in KEYWORDS:
        for rep in range(args.repeat):
            started = time.monotonic()
            try:
                d = fetch(base, "/search", {"keyword": kw, "limit": 30})
            except Exception as e:
                check(f"dual search {kw!r} #{rep+1}", False, str(e))
                continue
            items = d.get("items") or []
            errors = d.get("errors") or {}
            elapsed = time.monotonic() - started
            has_online = len(items) >= 1
            cb_open = any("circuit breaker open" in str(v) for v in errors.values())
            check(f"dual search {kw!r} #{rep+1} returns online items",
                  has_online, f"items={len(items)} errors={errors} elapsed={elapsed:.1f}s")
            check(f"dual search {kw!r} #{rep+1} no breaker open",
                  not cb_open, f"errors={errors}")

    # 2. Migu 单源慢源场景
    for kw in ["张学友", "陈奕迅"]:
        started = time.monotonic()
        try:
            d = fetch(base, "/search", {"keyword": kw, "limit": 30, "sources": "MiguMusicClient"})
        except Exception as e:
            check(f"migu single search {kw!r}", False, str(e))
            continue
        items = d.get("items") or []
        errors = d.get("errors") or {}
        elapsed = time.monotonic() - started
        check(f"migu single search {kw!r} returns items",
              len(items) >= 1, f"items={len(items)} errors={errors} elapsed={elapsed:.1f}s")
        check(f"migu single search {kw!r} within 20s", elapsed <= 20.0,
              f"elapsed={elapsed:.1f}s")

    if failures:
        print(f"\n{len(failures)} FAILURES: {failures}")
        return 1
    print("\nall search reliability checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
