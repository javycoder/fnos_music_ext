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
    UnsafeVmRequired,
    SUPPORTED_PLATFORMS,
    is_lxserver_unreachable,
    normalize_source,
    serialize_source_operation,
)

from proxy.quality_policy import QUALITY_LEVELS, to_lx_quality

VERIFY_KEYWORDS = ("晴天", "江南", "十年", "倔强")
_LX_QUALITY_ORDER = tuple(to_lx_quality(q) for q in reversed(QUALITY_LEVELS))
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


def _declared_qualities(source: dict, platform: str) -> list[str]:
    """只报告运行时显式声明档位；缺失元数据不等于支持全档。"""
    raw = source.get("sources") or source.get("platforms") or {}
    spec = raw.get(platform, {}) if isinstance(raw, dict) else {}
    values = spec.get("qualitys", spec.get("qualities", [])) if isinstance(spec, dict) else []
    if not values and isinstance(source.get("qualitys"), dict):
        values = source["qualitys"].get(platform, [])
    if not isinstance(values, (list, tuple)):
        return []
    return [q for q in _LX_QUALITY_ORDER if q in values]


def _verification_headers(lx_app, platform: str, resolved: dict) -> dict:
    """保持正式播放相同 UA/平台 Referer；不透传客户端认证或 Host。"""
    headers = {"User-Agent": lx_app.UA_PC}
    referer = {"kg": "https://www.kugou.com/", "tx": "https://y.qq.com/"}.get(platform)
    if referer:
        headers["Referer"] = referer
    source_headers = resolved.get("headers")
    source_headers = source_headers if isinstance(source_headers, dict) else {}
    for key, value in source_headers.items():
        if isinstance(key, str) and key.lower() in ("user-agent", "referer", "origin"):
            headers = {k: v for k, v in headers.items() if k.lower() != key.lower()}
            headers[key] = str(value)
    return headers


async def _verification_samples(lx_app, temp_client, sources, target_src, report, keywords):
    """顺序抽样且受调用方总超时约束；一项成功不会跳过其他平台。"""
    client = lx_app.get_http(lx_app.app)
    target_id = str(target_src.get("id") or "")
    excluded = [str(s["id"]) for s in sources if s.get("id") and str(s["id"]) != target_id]
    report["quality_results"] = {p: {} for p in report["platforms"]}
    for keyword in keywords[:4]:
        for platform in report["platforms"]:
            base = {"keyword": keyword, "platform": platform, "result": "", "error": ""}
            try:
                songs = await temp_client.search(keyword, source=platform, page=1, limit=3)
            except Exception as exc:
                if is_lxserver_unreachable(exc):
                    raise
                report["attempts"].append(dict(base, result="search_error", error=type(exc).__name__))
                continue
            if not songs:
                report["attempts"].append(dict(base, result="no_items"))
                continue
            declared = report["qualitys"][platform]
            # 真实上游list不一定公开品质声明；未知时探索代表高低档，
            # 但capability_known仍为false，不能把请求成功宣称为脚本全档能力。
            qualities = list(dict.fromkeys((declared[0], declared[-1]))) if declared else ["flac", "128k"]
            for quality in qualities:
                for song in songs[:3]:
                    attempt = dict(base, quality=quality, song_id=str(song.get("id") or ""),
                                   capability_known=bool(declared))
                    sinfo = temp_client.get_cached_song_info(song["id"]) or temp_client.synthesize_song_info(song["id"], song)
                    try:
                        resolved = await temp_client.get_music_url(
                            sinfo, quality, exclude_api_sources=excluded, auto_switch_api_source=False)
                    except Exception as exc:
                        if is_lxserver_unreachable(exc):
                            raise
                        report["attempts"].append(dict(attempt, result="resolve_failed", error=type(exc).__name__))
                        continue
                    if not resolved or not resolved.get("url"):
                        report["attempts"].append(dict(attempt, result="resolve_empty"))
                        continue
                    resolved_id = str(resolved.get("sourceId") or "")
                    if not target_id or resolved_id != target_id:
                        report["attempts"].append(dict(attempt, result="resolved_by_other_source"))
                        continue
                    headers = _verification_headers(lx_app, platform, resolved)
                    ok, final_url, content_type, size = await lx_app.probe_url(client, resolved["url"], headers)
                    report["attempts"].append(dict(attempt, result="ok" if ok else "probe_failed"))
                    previous = report["quality_results"][platform].get(quality)
                    report["quality_results"][platform][quality] = "ok" if ok or previous == "ok" else "failed"
                    if ok:
                        report["platform_results"][platform] = "ok"
                        report["ok"] = True
                        # 管理报告不发布完整临时签名链接，只保留媒体主机与格式。
                        report["probe"] = {"platform": platform, "song": song.get("title", ""),
                                           "host": urllib.parse.urlsplit(final_url).hostname or "",
                                           "ct": content_type, "size": size, "quality": quality}
                        break
                    if report["platform_results"][platform] != "ok":
                        report["platform_results"][platform] = "failed"


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
        except UnsafeVmRequired:
            report.update(category="unsafe_vm_required", requires_unsafe_vm=True, complete=False,
                          message="脚本需要不安全VM运行环境；本项目未自动授权，本次未验证")
            return report
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
        report["qualitys"] = {p: _declared_qualities(target_src, p) for p in declared}
        usable = declared
        report["platforms"] = usable
        report["platform_results"] = {p: "untested" for p in usable}

        if not usable:
            report.update(category="no_platform", message="源未声明任何支持的音乐平台")
            return report

        # 验证所有平台；总预算到期时保留已完成证据，不伪装成脚本失效。
        try:
            async with asyncio.timeout(_SAMPLE_BUDGET_S):
                await _verification_samples(lx_app, temp_client, sources, target_src, report, keywords)
        except TimeoutError:
            report.update(category="timeout", message="校验总预算已耗尽，未完成项保持未测", complete=False)
            return report
        report["complete"] = True
        if report["ok"]:
            report.update(category="partial" if any(v != "ok" for v in report["platform_results"].values()) else "ok",
                          message="抽样完成；各平台和音质结果请分别查看")
        else:
            report.update(category="resolve", message="当前样本未获得可播放媒体；请区分搜索、解析和探活阶段")
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
