"""lxmusic-service/verify_source.py
洛雪自定义源可用性端到端校验 (基于 lxserver 后端)。

校验链路：
1. 下载 / 读取脚本文本并校验头部元数据；
2. 作为临时源导入 lxserver 沙箱；
3. 读取声明的平台与音质；
4. 抽取标准关键词 (晴天、江南等) 发起搜索并调用解析 + Range 媒体探活；
5. 清理临时源并输出结构化校验报告。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Sequence
import urllib.parse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lxserver_client import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_LXSERVER_URL,
    LxServerClient,
    SUPPORTED_PLATFORMS,
    is_lxserver_unreachable,
    normalize_source,
    serialize_source_operation,
)

VERIFY_KEYWORDS = ("晴天", "江南", "十年", "倔强")
_PROBE_TIMEOUT = 12.0
_SAMPLE_BUDGET_S = 90.0
_CLEANUP_TIMEOUT_S = 3.0
_CLOSE_TIMEOUT_S = 1.0
logger = logging.getLogger("lxmusic_service")


def _new_temp_client(**kwargs) -> LxServerClient:
    """创建校验用临时 lxserver 客户端。"""
    return LxServerClient(**kwargs)

_NET_DEAD_MARKERS = (
    "ECONNREFUSED",
    "EHOSTUNREACH",
    "ENOTFOUND",
    "ETIMEDOUT",
    "EAI_AGAIN",
    "ECONNRESET",
    "ECONNABORTED",
    "fetch failed",
    "request timeout",
)


def _server_dead_reason(text: str) -> str | None:
    t = str(text or "")
    for marker in _NET_DEAD_MARKERS:
        if marker in t:
            if "ENOTFOUND" in t or "EAI_AGAIN" in t:
                return "源服务器的域名已不存在（已停止运营）"
            if "EHOSTUNREACH" in t:
                return "源服务器的 IP 从当前网络不可达（服务器下线或搬迁）"
            if "ECONNREFUSED" in t:
                return "源服务器拒绝连接（服务进程已关闭）"
            if marker in ("ETIMEDOUT", "ECONNRESET", "ECONNABORTED", "fetch failed", "request timeout"):
                return "连不上源脚本的服务器（超时或网络失败）"
    return None


def _looks_like_json_source(script: str) -> bool:
    raw = (script or "").lstrip()
    if not raw or raw[0] not in "{[":
        return False
    try:
        json.loads(script)
        return True
    except Exception:
        return False


async def download_script(url: str, timeout: float = 20.0) -> str:
    """下载或读取脚本内容。支持 http(s):// 与 file://"""
    url = url.strip()
    if url.startswith("file://"):
        raw = urllib.parse.unquote(url[len("file://"):])
        if os.name == "nt":
            if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
                raw = raw[1:]
            elif raw.startswith("///") and len(raw) > 4 and raw[4] == ":":
                raw = raw[3:]
        path = raw
        if not os.path.isfile(path):
            raise ValueError(f"本地文件不存在: {path}")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            raise ValueError(f"下载失败 (HTTP {resp.status_code})")
        return resp.text


def parse_script_meta(script: str) -> dict:
    meta = {"name": "", "description": "", "version": "1.0.0", "author": ""}
    # 与 lxserver extractMetadata 同契约：优先解析 /*! 或 /** 块注释
    block = re.search(r"/\*[*!]([\s\S]*?)\*/", script)
    lines = block.group(1).splitlines() if block else script.splitlines()[:50]
    for line in lines:
        line = line.strip()
        m = re.search(r"@(\w+)\s+(.+)", line)
        if m:
            key = m.group(1).lower()
            val = m.group(2).strip()
            if key in meta:
                meta[key] = val
    if not meta["name"]:
        # 从内容猜测
        meta["name"] = "custom_source"
    return meta


async def _cleanup_verification_client(
    client: LxServerClient, created_id: str | None, changed_reused_id: str | None,
) -> None:
    """在持锁的原 Task 内有界恢复；不能用 wait_for/shield 派生新锁 owner。"""
    try:
        async with asyncio.timeout(_CLEANUP_TIMEOUT_S):
            if created_id:
                if await client.delete_custom_source(created_id) is False:
                    raise RuntimeError("delete returned false")
            elif changed_reused_id:
                if await client.toggle_custom_source(changed_reused_id, False) is False:
                    raise RuntimeError("toggle returned false")
    except (Exception, asyncio.CancelledError) as exc:
        logger.warning(
            "verify_source cleanup failed (%s source %s): %s",
            "delete created" if created_id else "restore reused",
            created_id or changed_reused_id or "none", type(exc).__name__,
        )
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                await client.close()
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("verify_source client close failed: %s", type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise


@serialize_source_operation
async def verify_url(url: str, *, keywords: Sequence[str] | None = None) -> dict:
    """端到端校验一个源 URL 并返回结构化报告。"""
    import app as lx_app  # 延迟导入

    keywords = list(keywords) if keywords else list(VERIFY_KEYWORDS)
    report: dict = {
        "ok": False,
        "url": url,
        "category": "",
        "message": "",
        "meta": None,
        "declared_platforms": [],
        "platforms": [],
        "qualitys": {},
        "platform_results": {},
        "probe": None,
        "keywords": keywords,
        "attempts": [],
    }

    try:
        script = await download_script(url)
    except Exception as exc:
        report.update(category="download", message=str(exc))
        return report

    if _looks_like_json_source(script):
        report.update(
            category="format",
            message=(
                "不支持的音源格式：检测到 JSON 配置（musicApi.json 类 API 源）。"
                "本扩展只支持洛雪自定义音源 JS 脚本，请提供脚本文件本体或其 URL。"
            ),
        )
        return report

    meta = parse_script_meta(script)
    report["meta"] = meta

    temp_client = _new_temp_client(
        base_url=os.environ.get("LXSERVER_URL", DEFAULT_LXSERVER_URL),
        admin_password=os.environ.get("LXSERVER_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
        timeout=15.0,
    )

    temp_filename = f"verify_tmp_{int(time.time())}.js"
    # lxserver 上传后以脚本 @name 生成唯一 id；已存在同 id 源时复用既有源（测毕还原其启用态）
    temp_source_id: str | None = None
    changed_reused_id: str | None = None
    try:
        # 上传为临时源
        try:
            upload_res = await temp_client.upload_custom_source(temp_filename, script)
            temp_source_id = str(upload_res.get("id") or "") or None
        except Exception as upload_exc:
            if "已存在" not in str(upload_exc):
                # 内部通信故障上浮，不能归为脚本错误。
                if is_lxserver_unreachable(upload_exc):
                    raise
                report.update(category="internal", message=f"临时源上传失败: {upload_exc}")
                return report
        # 等待源在沙箱初始化并查询平台
        await asyncio.sleep(1.0)
        sources = await temp_client.list_custom_sources()
        target_src = None
        for s in sources:
            if (s.get("id") == temp_source_id if temp_source_id else s.get("name") == meta["name"]):
                target_src = s
                break
        if target_src is None:
            report.update(category="internal", message="临时源上传后未在 lxserver 列表中找到")
            return report

        # lxserver 解析只走 enabled 的源（isSourceSupported 跳过禁用源）：
        # 新上传的临时源默认禁用，须临时启用；复用既有源时记住原状态，测毕还原
        if temp_source_id is None:
            if not target_src.get("enabled"):
                reused_id = str(target_src.get("id") or "")
                if not reused_id:
                    report.update(category="internal", message="复用源缺少 ID，无法临时启用")
                    return report
                if await temp_client.toggle_custom_source(reused_id, True) is False:
                    report.update(category="internal", message="复用源临时启用失败")
                    return report
                changed_reused_id = reused_id
        else:
            await temp_client.toggle_custom_source(temp_source_id, True)

        declared = []
        # lxserver 列表项的平台字段是 supportedSources（数组）
        raw_platforms = target_src.get("supportedSources") or target_src.get("sources") or []
        if isinstance(raw_platforms, dict):
            raw_platforms = list(raw_platforms.keys())
        for p in raw_platforms:
            code = normalize_source(str(p))
            if code and code not in declared:
                declared.append(code)
        if not declared:
            declared = list(SUPPORTED_PLATFORMS)

        declared = [p for p in declared if p in SUPPORTED_PLATFORMS]
        report["declared_platforms"] = declared
        report["qualitys"] = {p: ["128k", "320k", "flac"] for p in declared}
        usable = declared
        report["platforms"] = usable
        report["platform_results"] = {p: "untested" for p in usable}

        if not usable:
            report.update(category="no_platform", message="源未声明任何支持的音乐平台")
            return report

        # 抽样测试
        client = lx_app.get_http(lx_app.app)
        target_id = str(target_src.get("id") or temp_source_id or "")
        other_sources = [
            str(s.get("id"))
            for s in sources
            if str(s.get("id") or "") and str(s.get("id") or "") != target_id
        ]
        for kw in keywords:
            for platform in usable:
                attempt = {"keyword": kw, "platform": platform, "result": "", "error": ""}
                try:
                    search_res = await temp_client.search(kw, source=platform, page=1, limit=3)
                except Exception as exc:
                    if is_lxserver_unreachable(exc):
                        raise
                    attempt.update(result="search_error", error=str(exc))
                    report["attempts"].append(attempt)
                    continue

                if not search_res:
                    attempt.update(result="no_items")
                    report["attempts"].append(attempt)
                    continue

                # 尝试解析第一首歌
                first_song = search_res[0]
                sinfo = temp_client.get_cached_song_info(first_song["id"]) or temp_client.synthesize_song_info(
                    first_song["id"], first_song
                )
                try:
                    url_res = await temp_client.get_music_url(
                        sinfo, "128k", exclude_api_sources=other_sources
                    )
                except Exception as exc:
                    if is_lxserver_unreachable(exc):
                        raise
                    attempt.update(result="resolve_failed", error=str(exc))
                    report["attempts"].append(attempt)
                    continue

                if not url_res or not url_res.get("url"):
                    attempt.update(result="resolve_empty")
                    report["attempts"].append(attempt)
                    continue

                # 校验解析归属：必须由待测源解析，防止被并发其他源代劳
                resolved_id = str(url_res.get("sourceId") or "")
                resolved_name = str(url_res.get("sourceName") or "")
                if (target_id and resolved_id and resolved_id != target_id) and (
                    resolved_name and resolved_name != meta["name"]
                ):
                    attempt.update(result="resolved_by_other_source")
                    report["attempts"].append(attempt)
                    continue

                # 探活
                probe_ok, final_url, ct, size = await lx_app.probe_url(client, url_res["url"])
                if probe_ok:
                    attempt.update(result="ok")
                    report["platform_results"][platform] = "ok"
                    report["attempts"].append(attempt)
                    report["ok"] = True
                    report["probe"] = {
                        "platform": platform,
                        "song": first_song.get("title", ""),
                        "url": final_url,
                        "ct": ct,
                        "size": size,
                    }
                    return report
                else:
                    attempt.update(result="probe_failed")
                    report["platform_results"][platform] = "failed"
                    report["attempts"].append(attempt)

        # 全部失败
        report.update(
            category="resolve",
            message="源脚本解析尝试均未成功，可能接口已被上游限制或凭据失效",
        )
        return report

    except Exception as exc:
        if not is_lxserver_unreachable(exc):
            raise
        report.update(
            ok=False, category="service_unavailable", stage="lxmusic_to_lxserver",
            message="洛雪脚本服务 lxserver 不可连接，请查看 lxserver 运行日志",
        )
        return report
    finally:
        # 只删除上传返回的本次创建 ID；既有源只恢复本次成功切换的状态。
        # timeout/cancel 仍在原 task/共享锁内完成有界清理，然后再向调用方传播。
        await _cleanup_verification_client(temp_client, temp_source_id, changed_reused_id)
