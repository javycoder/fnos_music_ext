"""HTTP wrapper for https://github.com/darknessomi/musicbox (NetEase-MusicBox CLI)."""
from __future__ import annotations

import io
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from netease_ext import (
    batch_song_details,
    check_is_logged_in,
    filter_playable_song_ids,
    playlist_track_ids,
    public_playlist,
    public_playlist_categories,
    public_playlist_tracks,
    public_playlists,
    reset_api,
    search_web_fallback,
    song_detail_raw,
    song_lyric_pair,
    vip_entitled,
    user_playlists as fetch_user_playlists,
)
import runner
import playback
from playback import get_song_url_async
from runner import MusicboxTimeoutError, ensure_xdg_dirs

# 与其他服务对齐：不配 handler 时 INFO 会被丢弃（只剩 lastResort 的 WARNING+）
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("musicbox_service.app")
try:  # 本地文件日志（logs/musicbox.log，按天滚动保留 3 天）；失败静默降级
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from proxy.fnlog import setup_logging as _setup_logging

    _setup_logging("musicbox_service.app", "musicbox")
except Exception:  # noqa: BLE001  无 proxy/ 上下文（如独立调试）时仅 stdout
    pass

ensure_xdg_dirs()

SEARCH_TYPES = {"song", "album", "artist", "playlist"}
# 统一官方七档支持：standard, higher, exhigh, lossless, hires, jymaster, sky
QUALITY_WHITELIST = {"standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky"}


class UpstreamException(Exception):
    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = (stderr or "")[:2000]


@asynccontextmanager
async def lifespan(application):
    try:
        yield
    finally:
        await playback.shutdown()


app = FastAPI(title="fnmusic-musicbox", version="1.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": exc.errors()})


@app.exception_handler(UpstreamException)
async def upstream_exception_handler(request, exc: UpstreamException):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": "upstream_error", "exit_code": exc.exit_code, "stderr": exc.stderr},
    )


@app.exception_handler(MusicboxTimeoutError)
async def timeout_exception_handler(request, exc: MusicboxTimeoutError):
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content={"detail": "Upstream musicbox command timed out"},
    )


def exec_musicbox(args: list[str], timeout: float = 30.0) -> Any:
    code, stdout, stderr = runner.run_musicbox(args, timeout=timeout)
    if code != 0:
        raise UpstreamException(exit_code=code, stderr=stderr or stdout or "")
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamException(exit_code=code, stderr=stderr or stdout or "") from exc


def _extract_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("ok") is True and "data" in payload:
        return payload["data"]
    return payload


def _parse_ids(ids_str: str | None) -> list[int]:
    if not ids_str or not ids_str.strip():
        raise HTTPException(status_code=422, detail="ids parameter is required")
    ids: list[int] = []
    for token in ids_str.split(","):
        token = token.strip()
        if not token:
            raise HTTPException(status_code=422, detail="Empty id in ids list")
        try:
            val = int(token)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid id {token!r}") from None
        if val <= 0:
            raise HTTPException(status_code=422, detail=f"Invalid id {token!r}")
        ids.append(val)
    if not 1 <= len(ids) <= 100:
        raise HTTPException(status_code=422, detail="ids count must be 1..100")
    return ids


@app.get("/healthz")
def healthz():
    return {"status": "ok", "source": "https://github.com/darknessomi/musicbox"}


@app.get("/api/v1/search")
def search(
    keyword: str = Query(...),
    type: str = Query("song"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword cannot be empty")
    if type not in SEARCH_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type {type!r}")

    if offset > 0:
        # CLI search 无 offset 参数；深分页直接走官方 web 搜索接口（原生分页）。
        try:
            page_items = search_web_fallback(keyword, stype=type, limit=limit, offset=offset)
        except Exception as exc:
            logger.warning("offset search failed: %s", exc)
            page_items = []
        if page_items and type == "song":
            song_ids = [it["song_id"] for it in page_items if it.get("song_id")]
            if song_ids:
                playable = filter_playable_song_ids(song_ids)
                filtered = [it for it in page_items if it.get("song_id") in playable]
                if filtered:
                    page_items = filtered
        return {"ok": True, "code": 200, "data": page_items}

    res = None
    fallback_needed = False

    try:
        res = exec_musicbox(["search", keyword, "--type", type, "--limit", str(limit), "--json"])
        if isinstance(res, dict):
            code = res.get("code")
            msg = str(res.get("message") or res.get("msg") or "")
            if code == 405 or res.get("ok") is False or "405" in msg or "频繁" in msg:
                fallback_needed = True
            elif type == "song":
                raw_list = res.get("data")
                if isinstance(raw_list, list):
                    def _song_id(item: dict) -> int:
                        try:
                            return int(item.get("song_id") or item.get("id") or 0)
                        except (ValueError, TypeError):
                            return 0

                    song_ids = [sid for it in raw_list if isinstance(it, dict) for sid in [_song_id(it)] if sid]
                    if song_ids:
                        playable = filter_playable_song_ids(song_ids)
                        res["data"] = [it for it in raw_list if isinstance(it, dict) and _song_id(it) in playable]
                    else:
                        res["data"] = []
                    if not res["data"]:
                        fallback_needed = True
                else:
                    fallback_needed = True
            elif not res.get("data"):
                fallback_needed = True
        else:
            fallback_needed = True
    except (UpstreamException, MusicboxTimeoutError, Exception) as exc:
        logger.warning("exec_musicbox search failed or blocked: %s, falling back to web endpoint", exc)
        fallback_needed = True

    if fallback_needed:
        try:
            fallback_items = search_web_fallback(keyword, stype=type, limit=limit)
            if fallback_items:
                if type == "song":
                    song_ids = [it["song_id"] for it in fallback_items if it.get("song_id")]
                    if song_ids:
                        playable = filter_playable_song_ids(song_ids)
                        filtered = [it for it in fallback_items if it.get("song_id") in playable]
                        if filtered:
                            fallback_items = filtered
                return {"ok": True, "code": 200, "data": fallback_items}
        except Exception as exc:
            logger.warning("fallback search failed: %s", exc)

    if res is not None and isinstance(res, dict) and "data" in res:
        return res
    if res is not None:
        return res
    return {"ok": True, "code": 200, "data": []}


@app.get("/api/v1/song/{song_id}/url")
async def song_url(song_id: int = Path(..., ge=1), quality: str = Query("exhigh"), fresh: int = Query(0),
                    purpose: str = Query("playback"), generation: int = Query(0, ge=0)):
    if quality not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    purpose = "download" if purpose.strip().lower() == "download" else "playback"
    try:
        item = await get_song_url_async(
            song_id, quality, fresh=bool(fresh), purpose=purpose, context_generation=generation)
        return {"ok": True, "data": item}
    except playback.ResolutionTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except playback.UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/song/{song_id}/info")
def song_info(song_id: int = Path(..., ge=1)):
    # 进程内直取优先（毫秒级）：CLI 子进程冷启动常超代理侧 4s 预算，导致
    # 封面/时长主路径超时回落。异常或空结果再降级 CLI 兜底。
    raw = song_detail_raw(song_id)
    if raw is not None:
        return {"ok": True, "data": raw}
    return exec_musicbox(["song", "info", str(song_id), "--json"])


@app.get("/api/v1/songs/detail")
def songs_detail(ids: str = Query(None)):
    parsed = _parse_ids(ids)
    try:
        return {"ok": True, "data": batch_song_details(parsed)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v1/song/{song_id}/lyric")
def song_lyric(song_id: int = Path(..., ge=1)):
    try:
        return {"ok": True, "data": song_lyric_pair(song_id)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v1/artist/{artist_id}")
def artist(artist_id: int = Path(..., ge=1), limit: int = Query(20, ge=1, le=100)):
    return exec_musicbox(["artist", str(artist_id), "--limit", str(limit), "--json"])


@app.get("/api/v1/album/{album_id}")
def album(album_id: int = Path(..., ge=1)):
    return exec_musicbox(["album", str(album_id), "--json"])


@app.get("/api/v1/playlist/{playlist_id}")
def playlist(playlist_id: int = Path(..., ge=1)):
    return exec_musicbox(["playlist", "show", str(playlist_id), "--json"])


@app.get("/api/v1/user/playlists")
def user_playlists(limit: int = Query(100, ge=1, le=100)):
    """网易账号歌单列表（需扫码登录；自建/收藏的判定字段随行下发，由调用方过滤）。"""
    res = fetch_user_playlists(limit)
    if res is None:
        return {"ok": False, "error": "not_logged_in", "logged_in": False}
    return {"ok": True, "data": res["playlists"], "account_uid": res["uid"], "logged_in": True}


@app.get("/api/v1/user/playlists/{playlist_id}/tracks")
def user_playlist_tracks(playlist_id: int = Path(..., ge=1)):
    """网易歌单曲目：trackIds -> 批量详情（不做可播过滤，VIP 曲目带 needs_vip 标记）。"""
    ids = playlist_track_ids(playlist_id)
    if ids is None:
        return {"ok": False, "error": "not_logged_in", "logged_in": False}
    return {"ok": True, "data": _mark_needs_vip(batch_song_details(ids[:1000]))}


def _cli_error_or_raise(exc: UpstreamException) -> Any:
    """CLI 非零退出时，若 stderr 带结构化 JSON 错误（如 not_logged_in）原样透传。"""
    try:
        parsed = json.loads(exc.stderr or "")
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        return parsed
    return None


def _mark_needs_vip(rows: list[dict]) -> list[dict]:
    """issue #57：VIP 歌曲行级标记——无 VIP 权益的账号遇到 fee==1（VIP 曲目）
    时附 needs_vip=True，代理据此在歌名前展示 [VIP] 前缀（用户点播即知只有试听）。

    有 VIP 权益的账号可完整播放，不打标。"""
    if vip_entitled():
        return rows
    for row in rows:
        if isinstance(row, dict) and row.get("fee") == 1:
            row["needs_vip"] = True
    return rows


def _playable_recommendation_rows(rows: Any, limit: int) -> list[dict]:
    """CLI 推荐输出 -> 批量详情 + 可播过滤（未登录剔除 VIP/试听片段）。

    2.8.0 起批量详情不再内联可播过滤（封面与可播性解耦），推荐行的可播
    过滤在此显式执行——推荐是可播列表用途，与纯展示封面不同。
    """
    ids: list[int] = []
    if isinstance(rows, list):
        for it in rows:
            if not isinstance(it, dict):
                continue
            try:
                sid = int(it.get("song_id") or it.get("id") or 0)
            except (ValueError, TypeError):
                sid = 0
            if sid > 0:
                ids.append(sid)
    hydrated = _mark_needs_vip(batch_song_details(ids[:100]))
    playable = filter_playable_song_ids([int(row["song_id"]) for row in hydrated])
    return [row for row in hydrated if int(row["song_id"]) in playable][:limit]


@app.get("/api/v1/recommend/songs")
def recommend_songs(limit: int = Query(30, ge=10, le=60)):
    """网易每日推荐（已登录为个性化推荐；匿名设备返回平台通用推荐）。"""
    try:
        data = exec_musicbox(["recommend", "songs", "--limit", str(limit), "--json"])
    except UpstreamException as exc:
        parsed = _cli_error_or_raise(exc)
        if parsed is not None:
            return parsed
        raise
    rows = _playable_recommendation_rows(_extract_payload(data), limit)
    return {"ok": True, "data": rows, "logged_in": check_is_logged_in()}


@app.get("/api/v1/toplist")
def toplist(index: int = Query(-1), limit: int = Query(60, ge=1, le=100)):
    """网易榜单：不带 index 返回榜单列表；带 index 返回该榜单可播曲目。"""
    if index < 0:
        return exec_musicbox(["toplist", "--json"])
    try:
        data = exec_musicbox(["toplist", "--index", str(index), "--json"])
    except UpstreamException as exc:
        parsed = _cli_error_or_raise(exc)
        if parsed is not None:
            return parsed
        raise
    rows = _playable_recommendation_rows(_extract_payload(data), limit)
    return {"ok": True, "data": rows, "index": index}


@app.get("/api/v1/public-playlists/capabilities")
def public_playlist_capabilities():
    logged_in = False
    try:
        logged_in = check_is_logged_in()
    except Exception:
        logged_in = False
    return {
        "ok": True,
        "provider": "musicbox",
        "sources": [{
            "id": "netease",
            "name": "网易云音乐",
            "playback_available": True,
            "channels": [
                {"id": "recommend", "name": "推荐歌单", "login_required": False},
                {"id": "top", "name": "热门与分类", "login_required": False},
                {"id": "highquality", "name": "精品歌单", "login_required": False},
                {"id": "search", "name": "搜索歌单", "login_required": False},
                {"id": "charts", "name": "排行榜", "login_required": False},
                {"id": "account_recommend", "name": "账号推荐歌单", "login_required": True},
            ],
            "logged_in": logged_in,
        }],
    }


@app.get("/api/v1/public-playlists/categories")
def public_playlist_categories_api(
    source: str = Query("netease"),
    kind: str = Query("playlist", pattern="^(playlist|chart)$"),
):
    if source not in ("netease", "wy", "163"):
        raise HTTPException(status_code=400, detail="unsupported source")
    try:
        if kind == "chart":
            return {
                "ok": True,
                "categories": [{"id": "", "name": "全部"}],
                "tags": [],
                "sorts": [],
            }
        data = public_playlist_categories("playlist")
        grouped = data.get("tags") if isinstance(data.get("tags"), dict) else {}
        data["categories"] = [{"id": "", "name": "全部"}] + [
            {"id": name, "name": name, "list": rows}
            for name, rows in grouped.items()
            if isinstance(rows, list)
        ]
        return data
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"netease categories failed: {exc}") from exc


@app.get("/api/v1/public-playlists")
def public_playlists_api(
    source: str = Query("netease"),
    channel: str = Query("top"),
    category: str = Query(""),
    sort: str = Query("hot"),
    keyword: str = Query(""),
    page: int = Query(1, ge=1, le=10000),
    limit: int = Query(30, ge=1, le=50),
    cursor: str = Query(""),
):
    if source not in ("netease", "wy", "163"):
        raise HTTPException(status_code=400, detail="unsupported source")
    channels = {"recommend", "top", "highquality", "search", "charts", "account_recommend"}
    if channel not in channels:
        raise HTTPException(status_code=400, detail=f"unsupported channel {channel}")
    if channel == "search" and not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword is required for search channel")
    try:
        return public_playlists(channel, category, sort, page, limit, cursor, keyword)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"netease playlists failed: {exc}") from exc


@app.get("/api/v1/public-playlists/{playlist_id}")
def public_playlist_api(
    playlist_id: int = Path(..., ge=1),
    source: str = Query("netease"),
    kind: str = Query("playlist", pattern="^(playlist|chart)$"),
):
    if source not in ("netease", "wy", "163"):
        raise HTTPException(status_code=400, detail="unsupported source")
    try:
        return public_playlist(playlist_id, kind)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"netease playlist failed: {exc}") from exc


@app.get("/api/v1/public-playlists/{playlist_id}/tracks")
def public_playlist_tracks_api(
    playlist_id: int = Path(..., ge=1),
    source: str = Query("netease"),
    kind: str = Query("playlist", pattern="^(playlist|chart)$"),
):
    if source not in ("netease", "wy", "163"):
        raise HTTPException(status_code=400, detail="unsupported source")
    try:
        result = public_playlist_tracks(playlist_id, kind)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"netease playlist tracks failed: {exc}") from exc
    result["data"] = _mark_needs_vip(result.get("data") or [])
    return result


@app.get("/api/v1/auth/status")
def auth_status():
    return exec_musicbox(["auth", "status", "--json"])


@app.post("/api/v1/auth/login")
def auth_login():
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    unikey = ""
    if isinstance(payload, dict):
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if unikey and isinstance(payload, dict):
        payload["qr_url"] = f"https://music.163.com/login?codekey={unikey}"
    return data


@app.get("/api/v1/auth/login/check")
def auth_login_check(unikey: str = Query(...)):
    if not unikey.strip():
        raise HTTPException(status_code=400, detail="unikey cannot be empty")
    data = exec_musicbox(["auth", "login", "--check", unikey, "--json"])
    # 扫码成功（803）时 CLI 子进程已把新 cookie 写盘，而常驻实例只在构造时
    # 读过盘：立即丢弃实例，让后续查询重建并读到新登录态
    payload = data.get("data") if isinstance(data, dict) else None
    if isinstance(payload, dict) and payload.get("code") == 803:
        reset_api("qr login success")
    return data


@app.get("/api/v1/auth/login/qr.png")
@app.get("/api/v1/auth/qr.png")
def auth_login_qr():
    try:
        import qrcode
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="qrcode extra not installed") from exc
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    unikey = ""
    if isinstance(payload, dict):
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if not unikey:
        raise UpstreamException(0, "Missing unikey in auth login response")
    qr_url = f"https://music.163.com/login?codekey={unikey}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/v1/auth/login/qr", response_class=Response)
@app.get("/api/v1/auth/qr", response_class=Response)
def auth_login_qr_text():
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    qr_ascii = ""
    unikey = ""
    if isinstance(payload, dict):
        qr_ascii = str(payload.get("qr_ascii") or "")
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if not qr_ascii:
        if not unikey:
            raise UpstreamException(0, "Missing unikey or qr_ascii in auth login response")
        qr_url = f"https://music.163.com/login?codekey={unikey}"
        try:
            import qrcode

            qr = qrcode.QRCode()
            qr.add_data(qr_url)
            qr.make(fit=True)
            f = io.StringIO()
            qr.print_ascii(out=f)
            qr_ascii = f.getvalue()
        except ImportError as exc:
            raise HTTPException(status_code=501, detail="qrcode extra not installed") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to render QR ascii: {exc}") from exc
    if not qr_ascii.endswith("\n"):
        qr_ascii += "\n"
    return Response(content=qr_ascii, media_type="text/plain; charset=utf-8")
