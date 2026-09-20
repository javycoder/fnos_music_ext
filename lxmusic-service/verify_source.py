"""洛雪自定义源可用性校验（安装向导 / WebUI / 运维共用）。

用法（服务环境内）:
    python3 verify_source.py <源URL>          # 人类可读输出，退出码 0=可用 1=不可用
    python3 verify_source.py <源URL> --json   # 仅输出 JSON 报告

校验链路：下载 → 头部/元数据校验 → Node 沙箱初始化 → 平台交集推导 →
内置搜索取一首热门歌 → musicUrl(128k) → Range 媒体探活。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from source_runtime import (  # noqa: E402
    MUSIC_PLATFORMS,
    SourceError,
    UserSource,
    build_music_info,
    download_script,
    parse_script_meta,
)

VERIFY_KEYWORD = "周杰伦 晴天"
_PROBE_TIMEOUT = 12.0


async def verify_url(url: str, *, keyword: str = VERIFY_KEYWORD) -> dict:
    """端到端校验一个源 URL；返回结构化报告（不改变当前激活源）。"""
    import app as lx_app  # 延迟导入：app 反向依赖本模块的时机只在端点内

    report: dict = {
        "ok": False, "url": url, "category": "", "message": "",
        "meta": None, "declared_platforms": [], "platforms": [], "qualitys": {},
        "probe": None,
    }
    try:
        script = await download_script(url)
    except SourceError as exc:
        report.update(category=exc.category, message=str(exc))
        return report
    meta = parse_script_meta(script)
    report["meta"] = meta

    runtime = UserSource(script, meta, script_dir=str(Path(__file__).resolve().parent))
    try:
        try:
            await runtime.start()
        except SourceError as exc:
            report.update(category=exc.category, message=str(exc))
            return report

        declared = runtime.music_platforms()
        report["declared_platforms"] = declared
        report["qualitys"] = {p: runtime.qualitys(p) for p in declared}
        usable = [p for p in MUSIC_PLATFORMS if p in declared and p in lx_app._SEARCHERS]
        report["platforms"] = usable
        if not usable:
            report.update(
                category="no_platform",
                message="源声明的平台与内置搜索平台无交集"
                        f"（源: {','.join(declared) or '无'}；内置: {','.join(sorted(lx_app._SEARCHERS))}）",
            )
            return report

        # 仅覆盖本任务的解析运行时：搜索期 VIP 探活走用户源，但不替换进程级 SOURCE_MANAGER
        override_token = lx_app._RUNTIME_OVERRIDE.set(runtime)
        # 搜索探活失败的异常被 _probe_candidates 静默吞掉；记录首个源解析错误，
        # 用于区分"真没搜到"和"源脚本解析全挂"（后者报搜索空会误导排查方向）
        probe_errors: list[str] = []
        original_music_url = runtime.music_url

        async def recording_music_url(music_info, quality, *, platform, timeout=10.0):
            try:
                return await original_music_url(music_info, quality, platform=platform, timeout=timeout)
            except SourceError as exc:
                if not probe_errors:
                    probe_errors.append(str(exc))
                raise

        runtime.music_url = recording_music_url
        client = lx_app.get_http(lx_app.app)
        try:
            item = None
            for platform in usable:
                try:
                    items = await asyncio.wait_for(
                        lx_app._SEARCHERS[platform](client, keyword, 3),
                        timeout=_PROBE_TIMEOUT,
                    )
                except Exception as exc:  # noqa: BLE001
                    report.setdefault("search_warnings", {})[platform] = str(exc)
                    continue
                if items:
                    item = dict(items[0])
                    report["search_platform"] = platform
                    break
            if item is None:
                if probe_errors:
                    report.update(
                        category="resolve",
                        message=f"源脚本解析失败，搜索探活全部未通过: {probe_errors[0]}",
                    )
                    return report
                report.update(
                    category="resolve",
                    message=f"内置搜索未在任何交集平台返回曲目（关键词: {keyword}）",
                )
                return report
            platform, identifier = lx_app.parse_track_id(str(item.get("id") or ""))
            item["_identifier"] = identifier
            quality = "128k"
            if quality not in runtime.qualitys(platform):
                declared_q = runtime.qualitys(platform)
                quality = declared_q[0] if declared_q else "128k"
            try:
                url_resolved = await asyncio.wait_for(
                    runtime.music_url(build_music_info(item, platform), quality, platform=platform),
                    timeout=_PROBE_TIMEOUT,
                )
            except SourceError as exc:
                # 脚本解析失败（含沙箱桥 API 缺失等运行时错误）是正常的不可用结论，不能向上抛穿端点
                report.update(category=exc.category, message=str(exc))
                return report
            ok, final_url, content_type, size = await lx_app.probe_url(client, url_resolved)
            if not ok:
                report.update(
                    category="resolve",
                    message=f"脚本返回的直链未通过媒体探活（HTTP 内容非音频）: {final_url[:120]}",
                )
                return report
            report["probe"] = {
                "platform": platform,
                "quality": quality,
                "title": str(item.get("title") or ""),
                "artist": str(item.get("artist") or ""),
                "content_type": content_type,
                "file_size": size,
            }
            report["ok"] = True
            return report
        finally:
            lx_app._RUNTIME_OVERRIDE.reset(override_token)
    finally:
        await runtime.stop()


def format_report(report: dict) -> str:
    meta = report.get("meta") or {}
    lines = [
        f"源名称   : {meta.get('name') or '-'}" + (f"  v{meta.get('version')}" if meta.get("version") else ""),
        f"作者     : {meta.get('author') or '-'}",
        f"可用平台 : {','.join(report.get('platforms') or []) or '-'}",
    ]
    if report.get("probe"):
        probe = report["probe"]
        lines.append(
            f"实测解析 : {probe['title']} - {probe['artist']} [{probe['platform']}/{probe['quality']}] "
            f"{probe.get('content_type') or ''} {probe.get('file_size') or 0} bytes"
        )
    if report.get("ok"):
        lines.append("结论     : 可用 ✓")
    else:
        lines.append(f"结论     : 不可用 ✗（{report.get('category') or 'unknown'}）{report.get('message') or ''}")
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--json"]
    as_json = "--json" in argv[1:]
    if len(args) != 1:
        print("用法: python3 verify_source.py <源URL> [--json]", file=sys.stderr)
        return 2
    report = await verify_url(args[0].strip())
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv)))
