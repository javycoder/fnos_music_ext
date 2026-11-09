#!/usr/bin/env python3
"""CT201 实机 smoke：歌单详情端点 + 页面结构（需在 CT201 上运行或通过 pct exec）。

验证：
  - 自定义歌单 import / detail / enable / delete 全链路；
  - 页面包含 playlist grid / detail panel，且不出现「进入编辑」/custom-list（variant 3 重构后）。
用法: python3 tests/integration/smoke_playlist_detail.py
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8774/app/fnmusic-ext"
HEADERS = {"X-Trim-Isadmin": "true", "Content-Type": "application/json"}


def call(path, data=None, method=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if data is None else json.dumps(data).encode(),
        headers=HEADERS,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {"raw": raw}
        return exc.code, body


def main():
    failures = []
    pid = None
    name = f"smoke-{int(time.time())}"

    def check(label, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'} {label}{' :: ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    status, body = call("/api/custom-playlists/does-not-exist")
    check("missing detail -> 404", status == 404, f"status={status}")

    status, body = call("/api/custom-playlists/bad.id")
    check("invalid id -> 404", status == 404, f"status={status}")

    content = "#EXTM3U\n#EXTINF:1,Artist A - Track A\na.mp3\n#EXTINF:2,Artist B - Track B\nb.mp3\n"
    status, body = call("/api/custom-playlists/import", {"filename": f"{name}.m3u", "content": content})
    pid = body.get("id")
    check("import -> 200 with id", status == 200 and bool(pid), f"status={status} id={pid}")

    if pid:
        status, body = call(f"/api/custom-playlists/{pid}")
        item = body.get("item") or {}
        tracks = item.get("tracks") or []
        check("detail -> 200", status == 200, f"status={status}")
        check("detail has 2 tracks", len(tracks) == 2, f"tracks={len(tracks)}")
        check("createdAt/updatedAt are Unix seconds",
              isinstance(item.get("createdAt"), int) and item["createdAt"] > 1_000_000_000
              and isinstance(item.get("updatedAt"), int) and item["updatedAt"] > 1_000_000_000,
              f"createdAt={item.get('createdAt')} updatedAt={item.get('updatedAt')}")

        status, body = call("/api/custom-playlists")
        ids = [row.get("id") for row in body.get("items") or []]
        check("list includes new id", status == 200 and pid in ids, f"status={status}")

        status, body = call(f"/api/custom-playlists/{pid}/enable", {"enabled": False})
        check("disable -> 200", status == 200, f"status={status}")
        status, body = call(f"/api/custom-playlists/{pid}")
        check("detail reflects disabled", status == 200 and (body.get("item") or {}).get("enabled") is False,
              f"status={status} enabled={(body.get('item') or {}).get('enabled')}")

        status, body = call(f"/api/custom-playlists/{pid}/enable", {"enabled": True})
        check("re-enable -> 200", status == 200, f"status={status}")

        status, body = call(f"/api/custom-playlists/{pid}?confirm=true", method="DELETE")
        check("delete -> 200", status == 200, f"status={status}")
        status, body = call(f"/api/custom-playlists/{pid}")
        check("deleted detail -> 404", status == 404, f"status={status}")

    req = urllib.request.Request(BASE + "/", headers={"X-Trim-Isadmin": "true"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode()
    check("page 200 has playlist grid", 'id="playlist-grid"' in html)
    check("page has detail panel", 'id="playlist-detail-panel"' in html)
    check("page has no 进入编辑", "进入编辑" not in html)
    check("page has no custom-list", 'id="custom-list"' not in html)

    if failures:
        print("FAILURES:", failures)
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
