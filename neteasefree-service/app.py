"""HTTP 包装：网易云音源（NeteaseCloudMusicApi 兼容 HTTP 后端）。

原实现包装 NetEase-MusicBox CLI；现直连兼容 NeteaseCloudMusicApi 的 HTTP 后端
（默认 https://zm.wwoyun.cn，可用 FNMUSIC_NETEASE_API_BASE 覆盖，见 netease_api.py），
对外 /api/v1/* 契约保持不变，代理侧无需改动。

关于登录：后端自带服务端登录态（免扫码即可解析 VIP 直链），因此不再有用户级扫码
登录；auth 路由保留原响应形状，/auth/status 反映后端可用性，登录类路由返回结构化
server_side_auth 以供上游优雅降级。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import netease_api as api
from netease_api import NeteaseApiError
from netease_ext import (
    album_songs,
    batch_song_details,
    category_rows,
    check_is_logged_in,
    filter_playable_song_ids,
    get_song_url,
    playlist_track_ids,
    recommend_rows,
    search_web_fallback,
    song_info as fetch_song_info,
    song_lyric_pair,
    toplist_list,
    toplist_rows,
    user_playlists as fetch_user_playlists,
)

logger = logging.getLogger("neteasefree_service.app")

SEARCH_TYPES = {"song", "album", "artist", "playlist"}
# 与其他源对齐的七档音质白名单：standard, higher, exhigh, lossless, hires, jymaster, sky
QUALITY_WHITELIST = {"standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky"}

SERVER_SIDE_AUTH_HINT = "本音源使用后端内置服务端登录态，无需扫码登录"


app = FastAPI(title="fnmusic-neteasefree", version="2.0.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": exc.errors()})


@app.exception_handler(NeteaseApiError)
async def upstream_exception_handler(request, exc: NeteaseApiError):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"ok": False, "error": "upstream_error", "detail": str(exc)},
    )


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


def _playable_rows(rows: list[dict]) -> list[dict]:
    """歌曲搜索行按可播性过滤（取链失败放行全部，避免一次抖动清空结果）。"""
    ids = [api._int(r.get("song_id")) for r in rows if api._int(r.get("song_id")) > 0]
    if not ids:
        return []
    playable = filter_playable_song_ids(ids)
    return [r for r in rows if api._int(r.get("song_id")) in playable]


@app.get("/healthz")
def healthz():
    return {"status": "ok", "source": "neteasefree", "api_base": api.api_base()}


@app.get("/api/v1/search")
def search(
    keyword: str = Query(...),
    type: str = Query("song"),
    limit: int = Query(20, ge=1, le=100),
):
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword cannot be empty")
    if type not in SEARCH_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type {type!r}")

    rows = search_web_fallback(keyword, stype=type, limit=limit)
    if type == "song":
        rows = _playable_rows(rows)
    return {"ok": True, "code": 200, "data": rows}


@app.get("/api/v1/song/{song_id}/url")
def song_url(song_id: int = Path(..., ge=1), quality: str = Query("exhigh")):
    if quality not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    item = get_song_url(song_id, quality)
    if item is None:
        return {"ok": False, "error": "not_playable"}
    return {"ok": True, "data": item}


@app.get("/api/v1/song/{song_id}/info")
def song_info(song_id: int = Path(..., ge=1)):
    obj = fetch_song_info(song_id)
    if obj is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "data": obj}


@app.get("/api/v1/songs/detail")
def songs_detail(ids: str = Query(None)):
    parsed = _parse_ids(ids)
    return {"ok": True, "data": batch_song_details(parsed)}


@app.get("/api/v1/song/{song_id}/lyric")
def song_lyric(song_id: int = Path(..., ge=1)):
    return {"ok": True, "data": song_lyric_pair(song_id)}


@app.get("/api/v1/artist/{artist_id}")
def artist(artist_id: int = Path(..., ge=1), limit: int = Query(20, ge=1, le=100)):
    obj, songs = api.artist_songs(artist_id, limit)
    ids = [api._int(s.get("id")) for s in songs if api._int(s.get("id")) > 0]
    return {"ok": True, "artist": obj, "data": batch_song_details(ids[:limit])}


@app.get("/api/v1/album/{album_id}")
def album(album_id: int = Path(..., ge=1)):
    # 原生曲目对象（含 ar/al/dt/sq/hr），由代理自行归一化
    return {"ok": True, "data": album_songs(album_id)}


@app.get("/api/v1/playlist/{playlist_id}")
def playlist(playlist_id: int = Path(..., ge=1)):
    detail = api.playlist_detail(playlist_id)
    if not detail:
        return {"ok": False, "error": "not_found"}
    songs = api.playlist_track_all(playlist_id, 1000)
    ids = [api._int(s.get("id")) for s in songs if api._int(s.get("id")) > 0]
    return {"ok": True, "playlist": detail, "data": batch_song_details(ids[:1000])}


@app.get("/api/v1/user/playlists")
def user_playlists(limit: int = Query(100, ge=1, le=100)):
    """后端无用户账号，恒返回未登录，由代理静默跳过「网易账号歌单」。"""
    res = fetch_user_playlists(limit)
    if res is None:
        return {"ok": False, "error": "not_logged_in", "logged_in": False}
    return {"ok": True, "data": res["playlists"], "account_uid": res["uid"], "logged_in": True}


@app.get("/api/v1/user/playlists/{playlist_id}/tracks")
def user_playlist_tracks(playlist_id: int = Path(..., ge=1)):
    ids = playlist_track_ids(playlist_id)
    if ids is None:
        return {"ok": False, "error": "not_logged_in", "logged_in": False}
    return {"ok": True, "data": batch_song_details(ids[:1000])}


@app.get("/api/v1/recommend/songs")
def recommend_songs(limit: int = Query(30, ge=10, le=60)):
    """网易推荐（后端登录态；不可用时回落平台通用推荐）。"""
    return {"ok": True, "data": recommend_rows(limit), "logged_in": check_is_logged_in()}


@app.get("/api/v1/toplist")
def toplist(index: int = Query(-1), limit: int = Query(60, ge=1, le=100)):
    """网易榜单：不带 index 返回榜单列表；带 index 返回该榜单可播曲目。"""
    if index < 0:
        return {"ok": True, "data": toplist_list()}
    return {"ok": True, "data": toplist_rows(index, limit), "index": index}


@app.get("/api/v1/top/playlist")
def top_playlist(cat: str = Query(""), limit: int = Query(100, ge=1, le=1000)):
    """歌单聚合：按网易分类（华语/流行/摇滚…）聚合热门歌单曲目（可播过滤后）。

    cat 留空=不限分类（全部热门歌单），用于每日推荐扩容补齐。
    """
    name = str(cat or "").strip()
    return {"ok": True, "data": category_rows(name, limit), "cat": name}


@app.get("/api/v1/auth/status")
def auth_status():
    ok = check_is_logged_in()
    return {
        "ok": True,
        "data": {
            "logged_in": ok,
            "nickname": "服务端账号" if ok else "",
            "user_id": 0,
            "mode": "server_side",
            "api_base": api.api_base(),
        },
    }


@app.post("/api/v1/auth/login")
def auth_login():
    return {"ok": False, "error": "server_side_auth", "message": SERVER_SIDE_AUTH_HINT}


@app.get("/api/v1/auth/login/check")
def auth_login_check(unikey: str = Query(...)):
    if not unikey.strip():
        raise HTTPException(status_code=400, detail="unikey cannot be empty")
    return {"ok": False, "error": "server_side_auth", "message": SERVER_SIDE_AUTH_HINT}


@app.get("/api/v1/auth/login/qr.png")
@app.get("/api/v1/auth/qr.png")
def auth_login_qr():
    raise HTTPException(status_code=409, detail=SERVER_SIDE_AUTH_HINT)


@app.get("/api/v1/auth/login/qr", response_class=Response)
@app.get("/api/v1/auth/qr", response_class=Response)
def auth_login_qr_text():
    raise HTTPException(status_code=409, detail=SERVER_SIDE_AUTH_HINT)