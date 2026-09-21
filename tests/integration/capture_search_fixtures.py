#!/usr/bin/env python3
"""录制 lxmusic 内置平台搜索器的真实上游响应，生成回放契约 fixture。

用途：lxmusic-service/test_searchers_replay.py 用这些真实响应回放验证
kg/wy/mg/tx/kw 五个搜索器的解析逻辑。当第三方接口改版导致搜索为空时，
先跑本脚本刷新 fixture，再修解析器——fixture 是"上游长什么样"的事实记录。

用法（需外网，手动 opt-in，不进 CI/pytest）：
    python3 tests/integration/capture_search_fixtures.py
    python3 tests/integration/capture_search_fixtures.py --out-dir /tmp/fixtures

请求参数与 lxmusic-service/app.py 各搜索器保持一致（改搜索器时同步改这里）。
每个响应保存原始字节 + meta.json 记录录制时间与校验结论。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "lxmusic-service" / "tests" / "fixtures"
KEYWORD = "晴天"
UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")


def fetch_all(client: httpx.Client) -> tuple[dict[str, bytes], dict[str, str]]:
    """返回 ({fixture名: 原始响应体}, {平台: 失败原因})。请求形状逐一对照各搜索器实现。

    逐平台容错：单个平台网络失败只记录原因，不阻断其余平台录制。
    """
    out: dict[str, bytes] = {}
    failures: dict[str, str] = {}

    # kg：mobilecdn v3 搜索（对照 kg_search）
    try:
        r = client.get(
            "http://mobilecdn.kugou.com/api/v3/search/song",
            params={"keyword": KEYWORD, "format": "json", "page": 1,
                    "pagesize": 30, "showtype": 1},
            headers={"User-Agent": UA_MOBILE},
        )
        r.raise_for_status()
        out["kg_search.json"] = r.content
    except Exception as exc:  # noqa: BLE001
        failures["kg_search.json"] = f"{type(exc).__name__}: {exc}"
    kg_raw = []
    if "kg_search.json" in out:
        kg_raw = (((json.loads(out["kg_search.json"]) or {}).get("data") or {}).get("info") or [])
    first_kg = next((it for it in kg_raw if isinstance(it, dict) and it.get("hash")), None)

    # kg 歌词两步（对照 kg_resolve_lyric）：搜索候选 → 下载 base64 lrc
    if first_kg:
        try:
            r = client.get(
                "https://krcs.kugou.com/search",
                params={"ver": 1, "man": "yes", "client": "mobi",
                        "keyword": f"{first_kg.get('songname','')} {first_kg.get('singername','')}".strip(),
                        "duration": int(first_kg.get("duration") or 0),
                        "hash": first_kg.get("hash")},
                headers={"User-Agent": UA_MOBILE},
            )
            r.raise_for_status()
            out["kg_lyric_search.json"] = r.content
            cand = ((r.json() or {}).get("candidates") or [None])[0]
            if cand:
                r = client.get(
                    "http://lyrics.kugou.com/download",
                    params={"ver": 1, "client": "pc", "id": cand.get("id"),
                            "accesskey": cand.get("accesskey"),
                            "fmt": "lrc", "charset": "utf8"},
                    headers={"User-Agent": UA_PC},
                )
                r.raise_for_status()
                out["kg_lyric_download.json"] = r.content
        except Exception as exc:  # noqa: BLE001
            failures["kg_lyric"] = f"{type(exc).__name__}: {exc}"

    # wy：music.163 web 搜索（对照 wy_search）
    try:
        r = client.post(
            "https://music.163.com/api/search/get/web",
            data={"s": KEYWORD, "type": 1, "offset": 0, "limit": 30, "total": "true"},
            headers={"User-Agent": UA_PC, "Referer": "https://music.163.com/",
                     "Cookie": "os=pc; appver=9.1.15"},
        )
        r.raise_for_status()
        out["wy_search.json"] = r.content
    except Exception as exc:  # noqa: BLE001
        failures["wy_search.json"] = f"{type(exc).__name__}: {exc}"
    wy_songs = []
    if "wy_search.json" in out:
        wy_songs = (((json.loads(out["wy_search.json"]) or {}).get("result") or {}).get("songs") or [])
    first_wy = next((it for it in wy_songs if isinstance(it, dict) and it.get("id")), None)

    # wy 歌词（对照 wy_resolve_lyric）
    if first_wy:
        try:
            r = client.get(
                "https://music.163.com/api/song/lyric",
                params={"id": first_wy["id"], "lv": 1, "tv": -1},
                headers={"User-Agent": UA_PC, "Referer": "https://music.163.com/",
                         "Cookie": "os=pc"},
            )
            r.raise_for_status()
            out["wy_lyric.json"] = r.content
        except Exception as exc:  # noqa: BLE001
            failures["wy_lyric"] = f"{type(exc).__name__}: {exc}"

    # mg：咪咕 search_all（对照 mg_search）
    try:
        r = client.get(
            "https://c.music.migu.cn/MIGUM2.0/v1.0/content/search_all.do",
            params={"text": KEYWORD, "pageNo": 1, "pageSize": 30, "resource": 1},
            headers={"User-Agent": UA_MOBILE, "Referer": "https://m.music.migu.cn/"},
        )
        r.raise_for_status()
        out["mg_search.json"] = r.content
    except Exception as exc:  # noqa: BLE001
        failures["mg_search.json"] = f"{type(exc).__name__}: {exc}"

    # tx：musicu.fcg 桌面搜索（对照 tx_search / TX_SEARCH_BODY）
    try:
        body = {"req_1": {
            "method": "DoSearchForQQMusicDesktop",
            "module": "music.search.SearchCgiService",
            "param": {"search_type": 0, "query": KEYWORD, "page_num": 1, "num_per_page": 30},
        }}
        r = client.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=body,
                        headers={"User-Agent": UA_PC, "Referer": "https://y.qq.com/"})
        r.raise_for_status()
        out["tx_search.json"] = r.content
    except Exception as exc:  # noqa: BLE001
        failures["tx_search.json"] = f"{type(exc).__name__}: {exc}"

    # kw：r.s 老接口（对照 kw_search；响应是 Python 字面量风格文本）
    try:
        r = client.get(
            "http://search.kuwo.cn/r.s",
            params={"all": KEYWORD, "ft": "music", "itemset": "web_2013",
                    "client": "kt", "pn": 0, "rn": 60,
                    "rformat": "json", "encoding": "utf8"},
            headers={"User-Agent": UA_PC, "Referer": "http://www.kuwo.cn/"},
        )
        r.raise_for_status()
        out["kw_search.txt"] = r.content
    except Exception as exc:  # noqa: BLE001
        failures["kw_search.txt"] = f"{type(exc).__name__}: {exc}"

    return out, failures


def sanity_check(name: str, body: bytes) -> str:
    """校验响应可被对应搜索器解析出候选；返回结论写入 meta。"""
    import ast

    try:
        if name == "kg_search.json":
            raw = (((json.loads(body) or {}).get("data") or {}).get("info") or [])
            n = sum(1 for it in raw if isinstance(it, dict) and it.get("hash"))
            return f"kg 候选 {n} 条" if n else "kg 候选为空（接口可能已改版）"
        if name == "wy_search.json":
            songs = (((json.loads(body) or {}).get("result") or {}).get("songs") or [])
            return f"wy 候选 {len(songs)} 条" if songs else "wy 候选为空（接口可能已改版）"
        if name == "mg_search.json":
            data = json.loads(body) or {}
            raw = data.get("songs") or (data.get("songResultData") or {}).get("result") or []
            return f"mg 候选 {len(raw)} 条" if raw else "mg 候选为空（接口可能已改版）"
        if name == "tx_search.json":
            songs = (((((json.loads(body) or {}).get("req_1") or {}).get("data") or {})
                      .get("body") or {}).get("song") or {}).get("list") or []
            return f"tx 候选 {len(songs)} 条" if songs else "tx 候选为空（接口可能已改版）"
        if name == "kw_search.txt":
            text = body.decode("utf-8", "replace").strip()
            data = json.loads(body) if text.startswith("{") and "'" not in text \
                else ast.literal_eval(text)
            raw = (data or {}).get("abslist") or []
            return f"kw 候选 {len(raw)} 条" if raw else "kw 候选为空（接口可能已改版）"
        if name == "kg_lyric_download.json":
            content = (json.loads(body) or {}).get("content") or ""
            decoded = base64.b64decode(content).decode("utf-8", "replace") if content else ""
            return "kg 歌词 base64 可解码" if decoded else "kg 歌词内容为空"
        if name == "wy_lyric.json":
            lrc = (json.loads(body) or {}).get("lrc") or {}
            return "wy 歌词非空" if lrc.get("lyric") else "wy 歌词为空"
        return "已保存（无校验规则）"
    except Exception as exc:  # noqa: BLE001
        return f"校验失败：{exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=FIXTURE_DIR,
                        help="fixture 输出目录（默认 lxmusic-service/tests/fixtures）")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, str] = {}
    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        captured, failures = fetch_all(client)

    for name, reason in failures.items():
        print(f"[SKIP] {name} 录制失败：{reason}", file=sys.stderr)
        meta[name] = f"录制失败：{reason}"
    for name, body in captured.items():
        (args.out_dir / name).write_bytes(body)
        note = sanity_check(name, body)
        meta[name] = note
        size = len(body)
        print(f"[OK] {name} ({size}B) → {note}")

    (args.out_dir / "meta.json").write_text(
        json.dumps({"captured_at": datetime.now(timezone.utc).isoformat(),
                    "keyword": KEYWORD, "notes": meta},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    empty = [n for n, note in meta.items()
             if "为空" in note or "失败" in note]
    if empty:
        print(f"\n[WARN] 以下响应缺失或未通过候选校验（接口改版/限流/网络不通）：{', '.join(empty)}",
              file=sys.stderr)
        print("对应的回放测试会跳过或失败——请先确认解析器是否需要更新。", file=sys.stderr)
    print(f"\n完成：{len(captured)} 个 fixture 已写入 {args.out_dir}")
    return 0 if captured else 1


if __name__ == "__main__":
    sys.exit(main())
