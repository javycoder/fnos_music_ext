"""lxmusic-service/playback_policy.py

洛雪播放策略的纯逻辑层。它解决三件事：

1. 音质阶梯：目标档位严格向下，不回弹。七档名称与洛雪音质字符串的映射
   由共享模块 ``proxy/quality_policy.py`` 统一；该模块尚未落盘时使用本文件
   内同契约的回退实现，主智能体接线后自动切换到共享模块。
2. 播放顺序：音质优先 -> 稳定脚本 ID 顺序 -> 同一脚本内原平台优先、
   其他声明平台其次。目标档位必须遍历完所有候选脚本后才允许降档。
3. 跨平台候选：只有歌名、歌手、版本标签、时长都可信时才允许替换；
   每平台最多留三个候选，搜索并发不超过二，结果缓存 60 秒。

模块不直接依赖 FastAPI 或 app 的全局状态，便于用假搜索器/假时钟做单元测试。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import sys
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
import unicodedata
from urllib.parse import urlsplit

logger = logging.getLogger("fnmusic.lxmusic.policy")

# 仓库根加入 sys.path，便于导入主智能体维护的共享质量模块。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from lxserver_client import SUPPORTED_PLATFORMS, normalize_source
except ImportError:  # pragma: no cover - 仅独立导入模块时兜底
    SUPPORTED_PLATFORMS = ("kw", "kg", "tx", "wy", "mg")
    _SOURCE_ALIASES = {
        "kugou": "kg", "kg": "kg", "netease": "wy", "163": "wy", "wy": "wy",
        "migu": "mg", "mg": "mg", "qq": "tx", "tencent": "tx", "tx": "tx",
        "kuwo": "kw", "kw": "kw",
    }

    def normalize_source(src: str | None) -> str:
        return _SOURCE_ALIASES.get(str(src or "").strip().lower(), "")


# --------------------------------------------------------------- 音质契约 --

# 统一七档，从低到高。与 REQUIREMENTS.md 和共享模块保持一致。
QUALITY_LEVELS: tuple[str, ...] = (
    "standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky",
)

_LX_QUALITY = {
    "standard": "128k",
    "higher": "192k",
    "exhigh": "320k",
    "lossless": "flac",
    "hires": "flac24bit",
    "jymaster": "master",
    "sky": "atmos",
}

_LX_TO_QUALITY = {value: key for key, value in _LX_QUALITY.items()}
# 兼容上游/旧缓存可能返回的别名，避免实际档位被误报成请求档位。
_LX_TO_QUALITY.update({
    "high": "exhigh", "hq": "exhigh",
    "standard": "standard", "lossless": "lossless",
    "hires": "hires", "jymaster": "jymaster", "sky": "sky",
    "mp3": "standard", "flac24": "hires",
})

_QUALITY_ALIASES = {
    "standard": "standard", "128k": "standard", "128": "standard", "low": "standard",
    "smooth": "standard",
    "higher": "higher", "192k": "higher", "192": "higher",
    "exhigh": "exhigh", "high": "exhigh", "hq": "exhigh",
    "320k": "exhigh", "320": "exhigh",
    "lossless": "lossless", "flac": "lossless", "sq": "lossless",
    "hires": "hires", "hr": "hires", "flac24bit": "hires", "24bit": "hires",
    "jymaster": "jymaster", "master": "jymaster", "母带": "jymaster",
    "sky": "sky", "atmos": "sky", "沉浸声": "sky",
}


def _fallback_normalize_quality(value: Any) -> str:
    """未知/缺省按播放默认档 lossless 处理；洛雪旧 high 固定为 exhigh。"""
    text = str(value or "").strip().lower()
    return _QUALITY_ALIASES.get(text, "lossless")


def _fallback_quality_ladder(target: Any) -> list[str]:
    quality = _fallback_normalize_quality(target)
    index = QUALITY_LEVELS.index(quality)
    return list(reversed(QUALITY_LEVELS[: index + 1]))


def _fallback_to_lx_quality(value: Any) -> str:
    return _LX_QUALITY.get(_fallback_normalize_quality(value), "128k")


# 共享模块存在时优先使用；接口缺失则整体回退，避免半套语义混用。
try:  # pragma: no cover - 由主智能体接线后覆盖
    from proxy.quality_policy import (  # type: ignore
        QUALITY_LEVELS as _shared_levels,
        normalize_quality as _shared_normalize_quality,
        quality_ladder as _shared_quality_ladder,
        to_lx_quality as _shared_to_lx_quality,
    )
except ImportError:
    normalize_quality = _fallback_normalize_quality
    quality_ladder = _fallback_quality_ladder
    to_lx_quality = _fallback_to_lx_quality
else:
    QUALITY_LEVELS = tuple(_shared_levels)
    normalize_quality = _shared_normalize_quality
    quality_ladder = _shared_quality_ladder
    to_lx_quality = _shared_to_lx_quality


def from_lx_quality(value: Any) -> str:
    """洛雪返回的 type（128k/flac/flac24bit/...）映射回统一档位；未知返回空串。"""
    text = str(value or "").strip().lower()
    return _LX_TO_QUALITY.get(text, "")


# --------------------------------------------------------------- 策略配置 --


@dataclass(frozen=True)
class PolicySettings:
    """三项 env 策略的不可变快照。"""

    auto_switch: bool = True
    platform_preferred: bool = False
    source_order: tuple[str, ...] = ()


def _parse_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _parse_source_order(raw: Any) -> tuple[str, ...]:
    """解析稳定脚本 ID JSON 数组；非法值只忽略，不阻断播放。"""
    if raw is None or raw == "":
        return ()
    if isinstance(raw, (list, tuple)):
        data: Any = list(raw)
    else:
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError):
            logger.warning("FNMUSIC_LX_SOURCE_ORDER 不是合法 JSON，已忽略")
            return ()
    if not isinstance(data, list):
        return ()
    order: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        sid = item.strip()
        if sid and sid not in order:
            order.append(sid)
    return tuple(order)


def read_policy_settings(values: Mapping[str, Any]) -> PolicySettings:
    """从 .env/进程环境映射读取三项策略；缺失分别使用 true/false/空顺序。"""
    return PolicySettings(
        auto_switch=_parse_bool(values.get("FNMUSIC_LX_AUTO_SWITCH"), True),
        platform_preferred=_parse_bool(values.get("FNMUSIC_LX_PLATFORM_PREFERRED"), False),
        source_order=_parse_source_order(values.get("FNMUSIC_LX_SOURCE_ORDER")),
    )


# --------------------------------------------------------------- 脚本顺序 --


@dataclass(frozen=True)
class ScriptInfo:
    """lxserver 自定义源的播放相关快照。"""

    id: str
    name: str
    ready: bool
    platforms: tuple[str, ...]
    enabled: bool = True
    status: str = ""


def build_source_catalog(sources: Sequence[dict] | None) -> list[ScriptInfo]:
    """把 lxserver 源列表映射成稳定目录；只有 enabled+status=success 才算就绪。

    缺失 status 表示上游仍在加载，不能默认成功。声明平台为空时沿用既有语义，
    视为支持全部平台。
    """
    catalog: list[ScriptInfo] = []
    seen: set[str] = set()
    for raw in sources or []:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("id") or "").strip()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        name = str(raw.get("name") or source_id).strip() or source_id
        enabled = bool(raw.get("enabled") or raw.get("enable"))
        status = str(raw.get("status") or "").strip().lower()
        ready = bool(enabled and status == "success")
        declared = raw.get("supportedSources") or raw.get("sources") or []
        if isinstance(declared, dict):
            declared = list(declared.keys())
        platforms: list[str] = []
        for platform in declared:
            normalized = normalize_source(str(platform))
            if normalized and normalized not in platforms:
                platforms.append(normalized)
        if not platforms:
            platforms = list(SUPPORTED_PLATFORMS)
        catalog.append(ScriptInfo(
            id=source_id,
            name=name,
            ready=ready,
            platforms=tuple(platforms),
            enabled=enabled,
            status=status,
        ))
    return catalog


def merge_source_order(catalog_ids: Iterable[str], preferred: Iterable[str] | None) -> list[str]:
    """按用户顺序排列现有脚本：删除的 ID 丢弃，新脚本按目录顺序追加。"""
    catalog: list[str] = []
    for item in catalog_ids or []:
        sid = str(item or "").strip()
        if sid and sid not in catalog:
            catalog.append(sid)
    allowed = set(catalog)
    order: list[str] = []
    for item in preferred or []:
        sid = str(item or "").strip()
        if sid in allowed and sid not in order:
            order.append(sid)
    for sid in catalog:
        if sid not in order:
            order.append(sid)
    return order


def merge_reorder(full_order: Sequence[str], submitted: Sequence[str]) -> list[str]:
    """把 WebUI 提交的可见（激活）脚本顺序合并回完整顺序。

    停用脚本不展示但必须保留历史位置：只替换被提交 ID 原本占据的槽位，
    未提交 ID 的位置不动。提交了完整顺序以外的新 ID 时追加到尾部。
    """
    full = [str(item) for item in full_order if str(item)]
    wanted = [str(item) for item in submitted if str(item)]
    wanted_set = set(wanted)
    slots = [index for index, sid in enumerate(full) if sid in wanted_set]
    result = list(full)
    for slot, sid in zip(slots, wanted):
        result[slot] = sid
    for sid in wanted[len(slots):]:
        if sid not in result:
            result.append(sid)
    # 去重，保留第一次出现的位置。
    deduped: list[str] = []
    for sid in result:
        if sid and sid not in deduped:
            deduped.append(sid)
    return deduped


def ordered_scripts(catalog: Sequence[ScriptInfo], order: Sequence[str] | None = None) -> list[ScriptInfo]:
    """按稳定 ID 顺序返回目录中的全部脚本（含停用，供 plan 过滤）。"""
    by_id = {script.id: script for script in catalog}
    ordered: list[ScriptInfo] = []
    for sid in merge_source_order(list(by_id), order or ()):
        script = by_id.get(sid)
        if script is not None:
            ordered.append(script)
    return ordered


# --------------------------------------------------------------- 播放计划 --


@dataclass(frozen=True)
class PlaybackStep:
    """一次「脚本 + 平台 + 目标档位」尝试。"""

    script_id: str
    script_name: str
    platform: str
    quality: str


def plan_steps(
    ladder: Sequence[str],
    scripts: Sequence[ScriptInfo],
    original_platform: str,
    *,
    auto_switch: bool = True,
    platform_preferred: bool = False,
) -> list[PlaybackStep]:
    """生成严格顺序的播放计划。

    外层是音质，内层是脚本，再内层是平台：同一目标档位必须遍历完全部
    就绪脚本后才下降一级。自动切换关闭时只选原平台首个就绪兼容脚本，
    并允许它自己逐级降档，不跨脚本、不跨平台。
    """
    tiers = [str(tier).strip() for tier in ladder if str(tier).strip()]
    if not tiers:
        return []
    original = normalize_source(original_platform)
    if not original:
        return []
    ready = [script for script in scripts if script.ready]

    if not auto_switch:
        chosen = next((script for script in ready if original in script.platforms), None)
        if chosen is None:
            return []
        return [
            PlaybackStep(chosen.id, chosen.name, original, tier)
            for tier in tiers
        ]

    steps: list[PlaybackStep] = []
    for tier in tiers:
        for script in ready:
            platforms: list[str] = []
            if original in script.platforms:
                platforms.append(original)
            if platform_preferred:
                for platform in script.platforms:
                    if platform != original and platform in SUPPORTED_PLATFORMS and platform not in platforms:
                        platforms.append(platform)
            for platform in platforms:
                steps.append(PlaybackStep(script.id, script.name, platform, tier))
    return steps


# --------------------------------------------------------- 可信候选匹配 --


_UNKNOWN_TITLES = {"未知曲目", "未知歌曲", "unknown", "unknown title"}
_UNKNOWN_ARTISTS = {"未知歌手", "未知艺术家", "unknown", "unknown artist"}
_BRACKET_RE = re.compile(r"[\(（\[\【]([^\)）\]\】]{1,40})[\)）\]\】]")
_PUNCT_RE = re.compile(r"[\s\-_·•・.,，。!！?？'\"“”‘’`~@#$%^&*+=|\\/<>:;；、]+")
_ARTIST_SPLIT_RE = re.compile(r"(?:feat\.?|ft\.?|with|&|、|,|，|/|;|；|\+)+", re.I)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# 版本标签规则：括号内或标题尾部命中即归一为同一标签。
_VERSION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("live", ("live", "现场", "演唱会", "concert")),
    ("instrumental", ("伴奏", "instrumental", "纯音乐", "offvocal", "off vocal", "钢琴版", "吉他版")),
    ("remix", ("remix", "混音", "dj版", "dj", "电音")),
    ("cover", ("cover", "翻唱", "翻自")),
    ("acoustic", ("acoustic", "不插电")),
    ("speed", ("加速", "speedup", "speed up", "spedup", "sped up")),
    ("slowed", ("降速", "slowed", "slowedreverb", "慢速")),
    ("clip", ("片段", "试听", "clip")),
)


def _nfkc(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def normalize_title(value: Any) -> str:
    """歌名归一：去掉括号附属信息、空白与标点，仅保留主体用于精确比较。"""
    text = _BRACKET_RE.sub(" ", _nfkc(value))
    return _PUNCT_RE.sub("", text).lower()


def _is_unknown(value: Any, pool: set[str]) -> bool:
    text = _nfkc(value).strip().lower()
    return not text or text in pool


def _fragment_tags(fragment: str) -> set[str]:
    """把版本片段归一为标签；feat/年份/普通专辑名不当作版本。"""
    text = _nfkc(fragment).lower()
    compact = re.sub(r"[\s\-_·•・]+", "", text)
    if not compact or _YEAR_RE.match(compact):
        return set()
    if "feat" in compact or compact.startswith("ft"):
        return set()
    tags: set[str] = set()
    for tag, words in _VERSION_RULES:
        if any(word in compact for word in words):
            tags.add(tag)
    if not tags and re.search(r"(版|mix|现场|伴奏)$", compact):
        # 未识别的「女声版」「器乐版」等：保留原文，避免与普通版混淆。
        tags.add(compact)
    return tags


def version_tags(value: Any) -> set[str]:
    """提取版本标签集合：括号片段 + 标题尾部后缀。"""
    text = _nfkc(value)
    tags: set[str] = set()
    for match in _BRACKET_RE.finditer(text):
        tags.update(_fragment_tags(match.group(1)))
    outside = _BRACKET_RE.sub(" ", text).strip()
    # 标题尾部后缀（如「夜曲 Live」「夜曲伴奏版」）；单独的「女声版」保留原文标签。
    tail = re.search(
        r"(?:(?:live|现场|演唱会|伴奏|纯音乐|remix|混音|dj|翻唱|cover|不插电|"
        r"acoustic|加速|降速|片段|试听)(?:版)?|[^\s]{0,6}版)$",
        outside,
        re.I,
    )
    if tail:
        tags.update(_fragment_tags(tail.group(0)))
    return tags


def _artist_parts(value: Any) -> list[str]:
    text = _BRACKET_RE.sub(" ", _nfkc(value))
    parts: list[str] = []
    for raw in _ARTIST_SPLIT_RE.split(text):
        normalized = _PUNCT_RE.sub("", raw).lower()
        if normalized and normalized not in _UNKNOWN_ARTISTS and normalized not in parts:
            parts.append(normalized)
    return parts


def normalize_artist(value: Any) -> str:
    return "/".join(_artist_parts(value))


def artists_compatible(target_artist: Any, candidate_artist: Any) -> bool:
    """歌手兼容：任一侧为主歌手名即可，缺关键身份信息直接拒绝。"""
    target = _artist_parts(target_artist)
    candidate = _artist_parts(candidate_artist)
    if not target or not candidate:
        return False
    for left in target:
        for right in candidate:
            if left == right:
                return True
            if len(left) >= 2 and len(right) >= 2 and (left in right or right in left):
                return True
    return False


def durations_compatible(target_duration: Any, candidate_duration: Any) -> bool:
    """双方时长已知时差值不超过 max(5 秒, 3%)；未知时长不据此否定。"""
    try:
        left = float(target_duration)
        right = float(candidate_duration)
    except (TypeError, ValueError):
        return True
    if left <= 0 or right <= 0:
        return True
    return abs(left - right) <= max(5.0, max(left, right) * 0.03)


def is_trusted_candidate(target: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """歌名一致、歌手兼容、版本标签一致、时长在阈值内才允许跨平台替换。"""
    if not isinstance(target, Mapping) or not isinstance(candidate, Mapping):
        return False
    target_title = target.get("title") or target.get("name")
    candidate_title = candidate.get("title") or candidate.get("name")
    if _is_unknown(target_title, _UNKNOWN_TITLES) or _is_unknown(candidate_title, _UNKNOWN_TITLES):
        return False
    normalized_target = normalize_title(target_title)
    if not normalized_target or normalized_target != normalize_title(candidate_title):
        return False
    if version_tags(target_title) != version_tags(candidate_title):
        return False
    if not artists_compatible(target.get("artist") or target.get("singer"),
                              candidate.get("artist") or candidate.get("singer")):
        return False
    return durations_compatible(target.get("duration_s"), candidate.get("duration_s"))


# --------------------------------------------------------- 媒体格式识别 --


_CONTENT_TYPE_EXT = {
    "mpeg": "mp3", "mp3": "mp3",
    "flac": "flac", "x-flac": "flac",
    "mp4": "m4a", "m4a": "m4a", "x-m4a": "m4a",
    "aac": "aac", "x-aac": "aac",
    "ogg": "ogg", "opus": "ogg", "x-ogg": "ogg",
    "wav": "wav", "x-wav": "wav", "wave": "wav", "vnd.wave": "wav",
}

_URL_EXT_ALIASES = {"opus": "ogg", "oga": "ogg", "m4b": "m4a"}


def media_ext_from_content_type(content_type: Any) -> str:
    """把探活返回的 MIME 映射为真实扩展名；未知返回空串，绝不默认 mp3。"""
    text = str(content_type or "").strip().lower().split(";", 1)[0].strip()
    if not text:
        return ""
    subtype = text.split("/", 1)[1] if "/" in text else text
    return _CONTENT_TYPE_EXT.get(subtype, "")


def media_ext_from_url(url: Any) -> str:
    """从最终直链后缀提取扩展名；只在路径最后一段有已知音频后缀时返回。"""
    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        return ""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1].lower()
    ext = _URL_EXT_ALIASES.get(ext, ext)
    return ext if ext in set(_CONTENT_TYPE_EXT.values()) else ""


# --------------------------------------------------------- 候选搜索缓存 --


# 搜索器契约：(平台, 关键词, 每平台拉取上限) -> lxserver 映射后的候选列表。
Searcher = Callable[[str, str, int], Awaitable[Sequence[Mapping[str, Any]]]]

_DEFAULT_SEARCH_LIMIT = 20


class CandidateFinder:
    """跨平台候选搜索：可信匹配、每平台 3 个、并发 2、60 秒缓存。

    `find` 自带同键在途合并；某个播放请求被取消时只影响它等待的 future，
    不取消别的调用方共享的搜索任务（独立搜索取消域）。
    """

    def __init__(
        self,
        *,
        ttl: float = 60.0,
        max_candidates: int = 3,
        concurrency: int = 2,
        max_entries: int = 256,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl = max(0.0, float(ttl))
        self.max_candidates = max(1, int(max_candidates))
        self.concurrency = max(1, int(concurrency))
        self.max_entries = max(8, int(max_entries))
        self._clock = clock or time.monotonic
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None

    def _sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.concurrency)
            self._semaphore_loop = loop
        return self._semaphore

    def invalidate(self) -> None:
        """配置/排序变化时主动失效，旧结果不再参与新计划。"""
        self._cache.clear()

    @staticmethod
    def _cache_key(item: Mapping[str, Any], platform: str, generation: int,
                   keyword: str) -> tuple:
        try:
            duration = int(float(item.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration = 0
        raw_title = item.get("title") or item.get("name")
        return (
            int(generation),
            platform,
            keyword,
            normalize_title(raw_title),
            tuple(sorted(version_tags(raw_title))),
            normalize_artist(item.get("artist") or item.get("singer")),
            duration,
        )

    def _cache_get(self, key: tuple, now: float) -> list[dict] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        timestamp, value = entry
        if now - timestamp > self.ttl:
            self._cache.pop(key, None)
            return None
        return [dict(row) for row in value]

    def _cache_put(self, key: tuple, value: list[dict], now: float) -> None:
        if len(self._cache) >= self.max_entries:
            oldest = sorted(self._cache, key=lambda k: self._cache[k][0])[: max(1, self.max_entries // 4)]
            for stale in oldest:
                self._cache.pop(stale, None)
        self._cache[key] = (now, [dict(row) for row in value])

    def _match(self, item: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict]:
        matched: list[dict] = []
        seen_ids: set[str] = set()
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            if not is_trusted_candidate(item, row):
                continue
            track_id = str(row.get("id") or "")
            if track_id and track_id in seen_ids:
                continue
            if track_id:
                seen_ids.add(track_id)
            matched.append(dict(row))
            if len(matched) >= self.max_candidates:
                break
        return matched

    async def find(
        self,
        searcher: Searcher,
        item: Mapping[str, Any],
        platform: str,
        deadline: float,
        generation: int = 0,
    ) -> list[dict]:
        """搜索并返回某平台最多三个可信候选；超时/异常返回空列表。"""
        normalized_platform = normalize_source(platform) or str(platform or "").strip()
        keyword = str(item.get("title") or item.get("name") or "").strip()
        if not normalized_platform or not keyword:
            return []
        now = self._clock()
        remaining = deadline - now
        if remaining <= 0:
            return []
        key = self._cache_key(item, normalized_platform, generation, keyword)
        cached = self._cache_get(key, now)
        if cached is not None:
            logger.debug("candidate cache hit platform=%s keyword=%s", normalized_platform, keyword)
            return cached
        existing = self._inflight.get(key)
        if existing is not None:
            # 跟随者有自己的总 deadline：不能无上限等领队的搜索任务。
            try:
                async with asyncio.timeout(remaining):
                    return await asyncio.shield(existing)
            except TimeoutError:
                logger.debug("candidate follower deadline platform=%s keyword=%s",
                             normalized_platform, keyword)
                return []

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        matched: list[dict] = []
        cacheable = False
        try:
            async with self._sem():
                remaining = deadline - self._clock()
                rows: Sequence[Mapping[str, Any]] = ()
                if remaining > 0:
                    try:
                        async with asyncio.timeout(remaining):
                            rows = await searcher(normalized_platform, keyword, _DEFAULT_SEARCH_LIMIT)
                    except TimeoutError:
                        # 预算耗尽不是平台/脚本失效，也不写负缓存。
                        logger.debug("candidate search timeout platform=%s keyword=%s",
                                     normalized_platform, keyword)
                    else:
                        cacheable = True
            matched = self._match(item, rows)
            if cacheable:
                self._cache_put(key, matched, self._clock())
            return matched
        except asyncio.CancelledError:
            if not future.done():
                future.set_exception(RuntimeError("candidate search cancelled"))
            future.exception()
            raise
        except Exception as exc:
            # 搜索是软失败：缺候选就回退到其他脚本/档位，不算脚本熔断。
            logger.debug("candidate search failed platform=%s keyword=%s: %s",
                         normalized_platform, keyword, exc)
            return []
        finally:
            if self._inflight.get(key) is future:
                self._inflight.pop(key, None)
            # 无论超时还是软失败都必须唤醒跟随者，否则它们会永远等这个 future。
            if not future.done():
                future.set_result(matched)

    async def find_many(
        self,
        searcher: Searcher,
        item: Mapping[str, Any],
        platforms: Iterable[str],
        deadline: float,
        generation: int = 0,
    ) -> dict[str, list[dict]]:
        """并发搜索多个平台（上限由构造函数 concurrency 控制），返回平台映射。"""
        wanted: list[str] = []
        for platform in platforms or []:
            normalized = normalize_source(platform) or str(platform or "").strip()
            if normalized and normalized not in wanted:
                wanted.append(normalized)
        if not wanted:
            return {}
        results = await asyncio.gather(
            *(self.find(searcher, item, platform, deadline, generation) for platform in wanted),
            return_exceptions=True,
        )
        found: dict[str, list[dict]] = {}
        for platform, result in zip(wanted, results):
            found[platform] = result if isinstance(result, list) else []
        return found
