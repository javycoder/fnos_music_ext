"""lxmusic-service/playback_policy.py

洛雪播放策略的纯逻辑层。它解决三件事：

1. 音质阶梯：目标档位严格向下，不回弹。七档名称、洛雪音质字符串映射和
   严格向下阶梯全部来自共享模块 ``proxy/quality_policy.py``，本文件不再
   重复定义，也不保留本地回退副本（两套语义容易走偏）。
2. 播放顺序：音质优先 -> 稳定脚本 ID 顺序 -> 同一脚本内原平台优先、
   其他声明平台其次。目标档位必须遍历完所有候选脚本后才允许降档；
   运行声明明确不支持的档位直接跳过，未声明（未知）的档位仍可探索。
3. 跨平台候选：只有歌名、歌手、版本标签、时长都可信时才允许替换；
   每平台最多留三个候选，搜索并发不超过二，结果缓存 60 秒。

模块不直接依赖 FastAPI 或 app 的全局状态，便于用假搜索器/假时钟做单元测试。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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

# 仓库根加入 sys.path，便于导入共享质量模块（容器镜像会 COPY proxy/）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lxserver_client import (  # noqa: E402  (必须先补 sys.path)
    SUPPORTED_PLATFORMS,
    normalize_source,
    parse_interval_to_seconds,
)
from proxy.quality_policy import (  # noqa: E402  (共享七档契约，禁止本地重复定义)
    QUALITY_LEVELS,
    normalize_quality,
    quality_ladder,
    to_lx_quality,
)


def quality_rank(tier: str) -> int:
    """档位序号（0 最低）；未知/空串返回 -1，调用方据此判断“档位未知”。"""
    try:
        return QUALITY_LEVELS.index(str(tier or ""))
    except ValueError:
        return -1


def from_lx_quality(value: Any) -> str:
    """洛雪返回的 type（128k/flac/flac24bit/...）映射回统一档位；未知返回空串。

    这里只认明确的洛雪档位与共享模块登记的别名。``mp3`` 这类只说明容器的
    字面量不能推出 128k——码率必须由上游报告，不能靠扩展名猜。
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    try:
        return normalize_quality(text, default=None)
    except ValueError:
        return ""


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
    """lxserver 自定义源的播放相关快照。

    ``qualities`` 是运行期声明的档位映射（平台 -> 明确支持的统一档位）。
    缺失某个平台表示未知，未知允许探索；只有明确声明不支持才跳过。
    """

    id: str
    name: str
    ready: bool
    platforms: tuple[str, ...]
    enabled: bool = True
    status: str = ""
    qualities: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    platforms_declared: bool = True  # 未声明时仅兼容原平台探索，不授权跨平台搜索。


def _quality_tiers(raw: Any) -> tuple[str, ...]:
    """把声明里的洛雪音质字符串/统一档位名解析成统一档位；不认识的忽略。"""
    if isinstance(raw, str):
        raw = [raw]
    tiers: list[str] = []
    for item in raw or ():
        text = str(item or "").strip()
        if not text:
            continue
        try:
            tier = normalize_quality(text, default=None)
        except ValueError:
            continue
        if tier not in tiers:
            tiers.append(tier)
    return tuple(tiers)


def _parse_platform_qualities(raw: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """读取 lxserver 源条目里的档位声明。

    兼容三种形态：``platforms: {kw: {qualitys: [...]}}``（运行期平台表）、
    ``qualitys/supportedQualities: {kw: [...]}``、以及只给一个列表时沿用到
    声明的全部平台。任何解析不出的形态都当作“未知”，不推断能力。
    """
    declared = raw.get("platforms")
    if declared is None:
        declared = raw.get("qualitys")
    if declared is None:
        declared = raw.get("supportedQualities")
    result: dict[str, tuple[str, ...]] = {}
    if isinstance(declared, Mapping):
        for platform, value in declared.items():
            normalized = normalize_source(str(platform))
            if not normalized:
                continue
            if isinstance(value, Mapping):
                value = value.get("qualitys") or value.get("qualities") or ()
            tiers = _quality_tiers(value)
            if tiers:
                result[normalized] = tiers
    elif isinstance(declared, (list, tuple)):
        tiers = _quality_tiers(declared)
        platforms = raw.get("supportedSources") or raw.get("sources") or ()
        if isinstance(platforms, Mapping):
            platforms = list(platforms)
        for platform in platforms:
            normalized = normalize_source(str(platform))
            if normalized and tiers:
                result[normalized] = tiers
    return result


def build_source_catalog(sources: Sequence[dict] | None) -> list[ScriptInfo]:
    """把 lxserver 源列表映射成稳定目录；只有 enabled+status=success 才算就绪。

    缺失 status 表示上游仍在加载，不能默认成功。声明平台为空时沿用既有语义，
    视为支持全部平台。档位声明缺失时保持未知，绝不补成固定三档。
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
        platforms_declared = bool(platforms)
        if not platforms:
            platforms = list(SUPPORTED_PLATFORMS)
        catalog.append(ScriptInfo(
            id=source_id,
            name=name,
            ready=ready,
            platforms=tuple(platforms),
            enabled=enabled,
            status=status,
            qualities=_parse_platform_qualities(raw),
            platforms_declared=platforms_declared,
        ))
    return catalog


def declared_supports_quality(declared: Any, tier: str) -> bool:
    """声明里是否明确包含该档位。

    解析不出任何档位（未声明 / 形态不认识）时返回 True：未知可以探索，
    只有“明确声明了支持列表且其中没有本档”才跳过。
    """
    tiers = _quality_tiers(declared)
    if not tiers:
        return True
    return str(tier) in tiers


def script_supports_quality(script: ScriptInfo, platform: str, tier: str) -> bool:
    """运行声明是否明确支持该档位；未声明返回 True（未知可探索，不能造能力）。"""
    normalized = normalize_source(platform) or str(platform or "")
    declared = script.qualities.get(normalized)
    if not declared:
        return True
    return str(tier) in declared


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

    运行期明确声明「不支持该档位」的脚本直接跳过该档位，省下预算给后续
    脚本；没有声明（未知）时仍然尝试，未知不等于不支持。
    """
    tiers = [str(tier).strip() for tier in ladder if str(tier).strip()]
    if not tiers:
        return []
    original = normalize_source(original_platform)
    if not original:
        return []
    ready = [script for script in scripts if script.ready]

    if not auto_switch:
        chosen = next(
            (script for script in ready
             if original in script.platforms),
            None,
        )
        if chosen is None:
            return []
        return [
            PlaybackStep(chosen.id, chosen.name, original, tier)
            for tier in tiers
            if script_supports_quality(chosen, original, tier)
        ]

    steps: list[PlaybackStep] = []
    for tier in tiers:
        for script in ready:
            platforms: list[str] = []
            if original in script.platforms:
                platforms.append(original)
            if platform_preferred and script.platforms_declared:
                for platform in script.platforms:
                    if platform != original and platform in SUPPORTED_PLATFORMS and platform not in platforms:
                        platforms.append(platform)
            for platform in platforms:
                if not script_supports_quality(script, platform, tier):
                    continue
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


def _title_of(row: Mapping[str, Any]) -> Any:
    """条目的歌名键兼容 title/name，避免各处重复取键。"""
    return row.get("title") or row.get("name")


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
    """歌手兼容：任一侧完整歌手名精确相等即可。

    禁止子串匹配：「周杰伦」与「周杰伦歌迷会」、「王菲」与「王菲菲」都是
    不同账号/不同人，旧实现用包含关系会造成假匹配。多歌手用、/&/feat 拆分，
    只要有一方列出的歌手与另一方完全相同就算兼容，缺关键身份信息直接拒绝。
    """
    target = _artist_parts(target_artist)
    candidate = _artist_parts(candidate_artist)
    if not target or not candidate:
        return False
    return any(left == right for left in target for right in candidate)


# 时长可能出现在这些键上：duration_s（标准条目）、duration（宽松上游）、
# interval（lxserver 原始 mm:ss）。三者都要显式解析，不能只看一个。
_DURATION_KEYS: tuple[str, ...] = ("duration_s", "duration", "interval")


def _duration_tolerance(*values: float) -> float:
    """双方时长都已知时的宽容阈值：不小于 5 秒，或较大值的 3%。"""
    known = [float(value) for value in values if float(value) > 0]
    return max(5.0, max(known) * 0.03) if known else 5.0


def duration_seconds(row: Mapping[str, Any] | None) -> float:
    """从 duration_s/duration/interval 显式取秒数；无法解析返回 0（未知）。"""
    if not isinstance(row, Mapping):
        return 0.0
    for key in _DURATION_KEYS:
        value = row.get(key)
        if value is None or value == "":
            continue
        seconds = parse_interval_to_seconds(value)
        if seconds > 0:
            return float(seconds)
    return 0.0


def duration_conflicts(row: Mapping[str, Any] | None) -> bool:
    """同一条目里多个时长键互相矛盾即“已知冲突”，不允许静默通过。"""
    if not isinstance(row, Mapping):
        return False
    values: list[float] = []
    for key in _DURATION_KEYS:
        value = row.get(key)
        if value is None or value == "":
            continue
        seconds = parse_interval_to_seconds(value)
        if seconds > 0:
            values.append(float(seconds))
    if len(values) < 2:
        return False
    return max(values) - min(values) > _duration_tolerance(*values)


def durations_compatible(target_duration: Any, candidate_duration: Any) -> bool:
    """双方时长已知时差值不超过 max(5 秒, 3%)；未知时长不据此否定。"""
    try:
        left = float(target_duration)
        right = float(candidate_duration)
    except (TypeError, ValueError):
        return True
    if left <= 0 or right <= 0:
        return True
    return abs(left - right) <= _duration_tolerance(left, right)


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
    # 任一条目自身时长键互相冲突时，时长不可信，不能当作“已知一致”放行。
    if duration_conflicts(target) or duration_conflicts(candidate):
        return False
    return durations_compatible(duration_seconds(target), duration_seconds(candidate))


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
        return (
            int(generation),
            platform,
            keyword,
            normalize_title(_title_of(item)),
            tuple(sorted(version_tags(_title_of(item)))),
            normalize_artist(item.get("artist") or item.get("singer")),
            int(duration_seconds(item)),
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
