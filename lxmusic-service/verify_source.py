"""lxmusic-service/verify_source.py
洛雪自定义源可用性端到端校验。

每次校验使用独立的临时数据目录和独立 SourceRuntimeManager。脚本只会在本次
校验创建的临时 Node 进程中执行，不会上传、启停或复用正在播放的同名源。
搜索仍复用共享 lxserver 的元数据接口，解析固定在本次测试源实例上。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
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
    UnsafeVmRequired,
    is_lxserver_unreachable,
    normalize_source,
    serialize_source_operation,
)
from source_runtime import (
    SourceInitializationFailed,
    SourceProcessBusy,
    SourceProcessUnavailable,
    SourceResolveTimeout,
    SourceRuntimeError,
    SourceRuntimeManager,
    SourceUnsafeVmRequired,
    parse_script_metadata,
)

from proxy.quality_policy import QUALITY_LEVELS, to_lx_quality

VERIFY_KEYWORDS = ("晴天", "江南", "十年", "倔强")
_LX_QUALITY_ORDER = tuple(to_lx_quality(q) for q in reversed(QUALITY_LEVELS))
_SAMPLE_BUDGET_S = 90.0
_VERIFY_BUDGET_S = 110.0
_RESOLVE_ATTEMPT_BUDGET_S = 20.0
_CLEANUP_TIMEOUT_S = 4.0
_CLOSE_TIMEOUT_S = 1.0
_MAX_SCRIPT_BYTES = 2 * 1024 * 1024
logger = logging.getLogger("lxmusic_service")


def _new_temp_client(**kwargs) -> LxServerClient:
    """临时元数据/搜索客户端；不承担源管理或解析。"""

    return LxServerClient(**kwargs)


def _new_temp_manager(source_root: str | Path, **kwargs) -> SourceRuntimeManager:
    """每次校验独立的管理器；调用方随后必须 close。"""

    return SourceRuntimeManager(
        source_root=source_root,
        init_timeout_s=min(20.0, _VERIFY_BUDGET_S),
        stop_budget_s=2.0,
        **kwargs,
    )


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
    """下载或读取脚本内容。支持 http(s):// 与 file://。"""

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
        if os.path.getsize(path) > _MAX_SCRIPT_BYTES:
            raise ValueError("本地脚本超过 2 MiB 上限")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as response:
            if response.status_code != 200:
                raise ValueError(f"下载失败 (HTTP {response.status_code})")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > _MAX_SCRIPT_BYTES:
                    raise ValueError("脚本超过 2 MiB 上限")
                chunks.extend(chunk)
    return chunks.decode("utf-8", errors="replace")


def parse_script_meta(script: str) -> dict:
    parsed = parse_script_metadata(script, "custom_source")
    return {
        "name": parsed.name,
        "description": parsed.description,
        "version": parsed.version,
        "author": parsed.author,
        "homepage": parsed.homepage,
    }


async def _cleanup_verification_runtime(
    manager: SourceRuntimeManager,
    client: LxServerClient,
    temp_root: str | Path,
) -> None:
    try:
        async with asyncio.timeout(_CLEANUP_TIMEOUT_S):
            await manager.close()
    except (Exception, asyncio.CancelledError) as exc:
        logger.warning("verify_source runtime cleanup failed: %s", type(exc).__name__)
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
        finally:
            shutil.rmtree(str(temp_root), ignore_errors=True)


def _declared_qualities(source: dict, platform: str) -> list[str]:
    """只报告运行时显式声明档位；缺失元数据不等于支持全档。"""

    values: object = []
    raw_quality = source.get("qualitys")
    if isinstance(raw_quality, dict):
        values = raw_quality.get(platform, [])
    if not values:
        raw = source.get("sources") or source.get("platforms") or {}
        spec = raw.get(platform, {}) if isinstance(raw, dict) else {}
        values = spec.get("qualitys", spec.get("qualities", [])) if isinstance(spec, dict) else []
    if not isinstance(values, (list, tuple)):
        return []
    return [q for q in _LX_QUALITY_ORDER if q in values]


def _declared_platforms(source: dict) -> list[str]:
    raw = source.get("supportedSources") or source.get("sources") or source.get("platforms") or []
    if isinstance(raw, dict):
        raw = list(raw.keys())
    result: list[str] = []
    for item in raw if isinstance(raw, (list, tuple, set)) else []:
        code = normalize_source(str(item))
        if code and code in SUPPORTED_PLATFORMS and code not in result:
            result.append(code)
    return result


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


async def _verification_samples(
    lx_app,
    metadata_client,
    manager: SourceRuntimeManager,
    target_id: str,
    report,
    keywords,
    deadline: float,
):
    """顺序抽样且受调用方总超时约束；一项成功不会跳过其他平台。"""

    client = lx_app.get_http(lx_app.app)
    target_id = str(target_id or "")
    report["quality_results"] = {p: {} for p in report["platforms"]}
    for keyword in keywords[:4]:
        for platform in report["platforms"]:
            base = {"keyword": keyword, "platform": platform, "result": "", "error": ""}
            try:
                songs = await metadata_client.search(keyword, source=platform, page=1, limit=3)
            except httpx.ReadTimeout:
                report["complete"] = False
                report["attempts"].append(dict(base, result="timeout", stage="search", error="ReadTimeout"))
                continue
            except Exception as exc:
                if is_lxserver_unreachable(exc):
                    raise
                report["attempts"].append(dict(base, result="search_error", error=type(exc).__name__))
                continue
            if not songs:
                report["attempts"].append(dict(base, result="no_items"))
                continue
            declared = report["qualitys"][platform]
            qualities = list(dict.fromkeys((declared[0], declared[-1]))) if declared else ["flac", "128k"]
            for quality in qualities:
                for song in songs[:3]:
                    attempt = dict(base, quality=quality, song_id=str(song.get("id") or ""),
                                   capability_known=bool(declared))
                    song_info = (
                        metadata_client.get_cached_song_info(song["id"])
                        or metadata_client.synthesize_song_info(song["id"], song)
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        report["complete"] = False
                        report["attempts"].append(dict(attempt, result="timeout", stage="resolve",
                                                       error="budget_exhausted"))
                        return
                    try:
                        resolved = await manager.resolve(
                            target_id,
                            song_info,
                            quality,
                            deadline=min(deadline, time.monotonic() + _RESOLVE_ATTEMPT_BUDGET_S),
                        )
                    except (SourceResolveTimeout, TimeoutError):
                        report["complete"] = False
                        report["attempts"].append(dict(attempt, result="timeout", stage="resolve",
                                                       error="TimeoutError"))
                        continue
                    except SourceProcessBusy:
                        report["complete"] = False
                        report["attempts"].append(dict(attempt, result="timeout", stage="resolve",
                                                       error="source_process_busy"))
                        continue
                    except (SourceProcessUnavailable, SourceRuntimeError) as exc:
                        report["attempts"].append(dict(attempt, result="resolve_failed",
                                                       error=getattr(exc, "code", type(exc).__name__)))
                        continue
                    except httpx.ReadTimeout:
                        report["complete"] = False
                        report["attempts"].append(dict(attempt, result="timeout", stage="resolve",
                                                       error="ReadTimeout"))
                        continue
                    except Exception as exc:
                        if is_lxserver_unreachable(exc):
                            raise
                        report["attempts"].append(dict(attempt, result="resolve_failed",
                                                       error=type(exc).__name__))
                        continue
                    if not resolved or not resolved.get("url"):
                        report["attempts"].append(dict(attempt, result="resolve_empty"))
                        continue
                    resolved_id = str(resolved.get("sourceId") or "")
                    if not target_id or resolved_id != target_id:
                        report["attempts"].append(dict(attempt, result="resolved_by_other_source"))
                        continue
                    headers = _verification_headers(lx_app, platform, resolved)
                    try:
                        ok, final_url, content_type, size = await lx_app.probe_url(
                            client, resolved["url"], headers
                        )
                    except Exception as exc:
                        ok, final_url, content_type, size = False, resolved["url"], "", 0
                        report["attempts"].append(dict(attempt, result="probe_failed",
                                                       error=type(exc).__name__))
                    else:
                        report["attempts"].append(dict(attempt, result="ok" if ok else "probe_failed"))
                    previous = report["quality_results"][platform].get(quality)
                    report["quality_results"][platform][quality] = (
                        "ok" if ok or previous == "ok" else "failed"
                    )
                    if ok:
                        report["platform_results"][platform] = "ok"
                        report["ok"] = True
                        report["probe"] = {
                            "platform": platform,
                            "song": song.get("title", ""),
                            "host": urllib.parse.urlsplit(final_url).hostname or "",
                            "ct": content_type,
                            "size": size,
                            "quality": quality,
                        }
                        break
                    if report["platform_results"][platform] != "ok":
                        report["platform_results"][platform] = "failed"


@serialize_source_operation
async def verify_url(url: str, *, keywords: Sequence[str] | None = None, quick: bool = False) -> dict:
    """端到端校验一个源 URL 并返回结构化报告。

    quick=True 只执行下载、格式、初始化、声明平台检查，跳过搜索与媒体抽样。
    """

    deadline = time.monotonic() + _VERIFY_BUDGET_S
    import app as lx_app

    keywords = list(keywords) if keywords else list(VERIFY_KEYWORDS)
    stage = "download"
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
        "source_id": "",
        "complete": False,
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

    report["meta"] = parse_script_meta(script)
    temp_root = Path(tempfile.mkdtemp(prefix="fnmusic-lx-verify-"))
    metadata_client = _new_temp_client(
        base_url=os.environ.get("LXSERVER_URL", DEFAULT_LXSERVER_URL),
        admin_password=os.environ.get("LXSERVER_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
        timeout=15.0,
    )
    manager: SourceRuntimeManager | None = None
    try:
        manager = _new_temp_manager(temp_root / "_open")
        stage = "start"
        await manager.start()
        filename = Path(urllib.parse.urlsplit(url).path).name or "verify_source.js"
        stage = "upload"
        record = await manager.upload_source(
            filename,
            script,
            deadline=min(deadline, time.monotonic() + 20.0),
        )
        target_id = str(record.get("id") or "")
        report["source_id"] = target_id
        report["meta"] = {
            "name": str(record.get("name") or report["meta"].get("name") or "custom_source"),
            "description": str(record.get("description") or ""),
            "version": str(record.get("version") or "1.0.0"),
            "author": str(record.get("author") or ""),
            "homepage": str(record.get("homepage") or ""),
        }
        declared = _declared_platforms(record)
        report["declared_platforms"] = declared
        report["qualitys"] = {p: _declared_qualities(record, p) for p in declared}
        usable = declared
        report["platforms"] = usable
        report["platform_results"] = {p: "untested" for p in usable}
        if not usable:
            report.update(category="no_platform", message="源未声明任何支持的音乐平台")
            return report
        if quick:
            report.update(
                ok=True,
                complete=True,
                category="quick_gate",
                message=f"快速门禁校验通过，声明支持平台：{', '.join(usable)}",
            )
            return report

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report.update(category="timeout", complete=False,
                          message="校验总预算在初始化后已耗尽；未执行搜索/解析")
            return report
        stage = "enable"
        await manager.set_enabled(
            target_id, True, deadline=min(deadline, time.monotonic() + 20.0)
        )
        stage = "sample"
        sample_budget = min(_SAMPLE_BUDGET_S, deadline - time.monotonic())
        report["complete"] = sample_budget > 0
        if sample_budget > 0:
            try:
                async with asyncio.timeout(sample_budget):
                    await _verification_samples(
                        lx_app,
                        metadata_client,
                        manager,
                        target_id,
                        report,
                        keywords,
                        deadline,
                    )
            except TimeoutError:
                report["complete"] = False
        if not report["complete"]:
            report.update(category="partial" if report["ok"] else "timeout",
                          message="校验发生读超时或总预算耗尽；保留已完成证据，未完成项不判失效")
        elif report["ok"]:
            report.update(category="partial" if any(v != "ok" for v in report["platform_results"].values()) else "ok",
                          message="抽样完成；各平台和音质结果请分别查看")
        else:
            report.update(category="resolve", message="当前样本未获得可播放媒体；请区分搜索、解析和探活阶段")
        return report
    except SourceUnsafeVmRequired:
        report.update(category="unsafe_vm_required", requires_unsafe_vm=True, complete=False,
                      message="脚本需要不安全VM运行环境；本项目未自动授权，本次未验证")
        return report
    except SourceInitializationFailed as exc:
        report.update(category="initialization", complete=False,
                      message=f"临时源初始化失败: {exc.code}")
        return report
    except SourceRuntimeError as exc:
        report.update(category="runtime", complete=False,
                      message=f"临时源运行时错误: {exc.stage}: {exc.code}")
        return report
    except Exception as exc:
        if isinstance(exc, httpx.ReadTimeout):
            report.update(ok=False, category="timeout", stage=stage, complete=False,
                          message="临时源校验读超时；结果未知，未重放")
            if getattr(exc, "result_unknown", False):
                report["result_unknown"] = True
            return report
        if not is_lxserver_unreachable(exc):
            raise
        report.update(
            ok=False, category="service_unavailable", stage="lxmusic_to_lxserver",
            complete=False,
            message="洛雪脚本服务 lxserver 不可连接，请查看 lxserver 运行日志",
        )
        if getattr(exc, "result_unknown", False):
            report["result_unknown"] = True
        return report
    finally:
        if manager is not None:
            await _cleanup_verification_runtime(manager, metadata_client, temp_root)
        else:
            await metadata_client.close()
            shutil.rmtree(str(temp_root), ignore_errors=True)
