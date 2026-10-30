"""各音源共享的音质规则；本模块不执行网络请求或修改用户配置。

官方档位是选择优先级，不是实际媒体品质证明。旧 App 的 standard 代表
320k，必须由 app_download_quality 单独解释，不能直接当作新标准 128k。
"""
from __future__ import annotations

from collections.abc import Mapping

QUALITY_LEVELS = ("standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky")
QUALITY_LABELS = {
    "standard": "标准", "higher": "较高", "exhigh": "极高", "lossless": "无损",
    "hires": "Hi-Res", "jymaster": "超清母带", "sky": "沉浸声",
}
LX_QUALITIES = dict(zip(QUALITY_LEVELS, ("128k", "192k", "320k", "flac", "flac24bit", "master", "atmos")))
_QUALITY_ALIASES = {value: key for key, value in LX_QUALITIES.items()}
_QUALITY_ALIASES.update({
    "128": "standard", "192": "higher", "320": "exhigh", "high": "exhigh",
    "hq": "exhigh", "sq": "lossless", "hr": "hires", "flac24": "hires",
})
_TRUE_VALUES = {"true", "1", "yes", "on"}


def normalize_quality(value: object, default: str | None = "lossless") -> str:
    """规范化平台别名；default=None 用于严格校验，不猜测未来档位。"""
    text = str(value or "").strip().lower()
    normalized = _QUALITY_ALIASES.get(text, text)
    if normalized in QUALITY_LEVELS:
        return normalized
    if default is None:
        raise ValueError(f"不支持的音质档位: {text!r}")
    return normalize_quality(default, default=None)


def quality_ladder(quality: str) -> list[str]:
    """只返回所选档位及以下候选；不存在先降后升的隐式策略。"""
    selected = normalize_quality(quality)
    return list(reversed(QUALITY_LEVELS[:QUALITY_LEVELS.index(selected) + 1]))


def to_lx_quality(quality: str) -> str:
    """将官方档位转换为洛雪脚本请求参数，保留七档差异。"""
    return LX_QUALITIES[normalize_quality(quality)]


def effective_play_quality(values: Mapping[str, object]) -> str:
    """优先新配置；兼容旧偏好时复现当前提供者原先的首选档位。"""
    explicit = str(values.get("FNMUSIC_PLAY_QUALITY") or "").strip()
    if explicit:
        return normalize_quality(explicit)
    mode = str(values.get("FNMUSIC_QUALITY_MODE") or "high").strip().lower()
    is_lx = str(values.get("FNMUSIC_LX_ENABLED") or "").strip().lower() in _TRUE_VALUES
    if mode == "smooth":
        return "standard"
    if mode == "balanced":
        # 旧洛雪三档中位是320k；旧网易六档的中间偏高是无损。
        return "exhigh" if is_lx else "lossless"
    key = "FNMUSIC_LX_QUALITY" if is_lx else "FNMUSIC_NETEASE_QUALITY"
    return normalize_quality(values.get(key) or "lossless")


def effective_download_quality(values: Mapping[str, object]) -> str:
    """旧下载standard按320k迁移，避免升级后意外降低到128k。"""
    explicit = str(values.get("FNMUSIC_DOWNLOAD_QUALITY") or "").strip().lower()
    if explicit:
        return "app" if explicit == "app" else normalize_quality(explicit)
    legacy = str(values.get("FNMUSIC_DL_QUALITY") or "app").strip().lower()
    return {"app": "app", "original": "lossless", "standard": "exhigh"}.get(legacy, "app")


def app_download_quality(raw: object, is_original: bool, play_quality: str) -> str:
    """解释官方下载协议；未知值沿用播放档，显式原始档才请求最高可用。"""
    text = str(raw or "").strip().lower()
    if is_original or text in {"original", "原始", "原文件"}:
        return QUALITY_LEVELS[-1]
    if text in {"standard", "std", "mp3", "标准"}:
        return "exhigh"
    if text in QUALITY_LEVELS or text in _QUALITY_ALIASES:
        return normalize_quality(text)
    return normalize_quality(play_quality)


def quality_migration(values: Mapping[str, object]) -> dict[str, str]:
    """只建议缺失的新键；调用方通过现有安全配置工具原子持久化。"""
    updates = {}
    if not str(values.get("FNMUSIC_PLAY_QUALITY") or "").strip():
        updates["FNMUSIC_PLAY_QUALITY"] = effective_play_quality(values)
    if not str(values.get("FNMUSIC_DOWNLOAD_QUALITY") or "").strip():
        updates["FNMUSIC_DOWNLOAD_QUALITY"] = effective_download_quality(values)
    return updates
