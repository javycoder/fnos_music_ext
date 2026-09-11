"""HTTP wrapper for https://github.com/darknessomi/musicbox (NetEase-MusicBox CLI)."""
from __future__ import annotations

import io
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from netease_ext import batch_song_details, check_is_logged_in, filter_playable_song_ids, song_lyric_pair
import runner
from runner import MusicboxTimeoutError, ensure_xdg_dirs

ensure_xdg_dirs()

SEARCH_TYPES = {"song", "album", "artist", "playlist"}
QUALITY_WHITELIST = {"exhigh", "higher", "standard", "lossless", "hires", "jymaster"}


class UpstreamException(Exception):
    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = (stderr or "")[:2000]


app = FastAPI(title="fnmusic-musicbox", version="1.0.0")


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
):
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword cannot be empty")
    if type not in SEARCH_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type {type!r}")
    res = exec_musicbox(["search", keyword, "--type", type, "--limit", str(limit), "--json"])
    if type == "song" and isinstance(res, dict):
        raw_list = res.get("data")
        if isinstance(raw_list, list):

            def _song_id(item: dict) -> int:
                # 畸形数据（非数字 id）一律归零，零不可能命中 playable 集合
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
    return res


@app.get("/api/v1/song/{song_id}/url")
def song_url(song_id: int = Path(..., ge=1), quality: str = Query("exhigh")):
    if quality not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    return exec_musicbox(["song", "url", str(song_id), "--quality", quality, "--json"])


@app.get("/api/v1/song/{song_id}/info")
def song_info(song_id: int = Path(..., ge=1)):
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


def _cli_error_or_raise(exc: UpstreamException) -> Any:
    """CLI 非零退出时，若 stderr 带结构化 JSON 错误（如 not_logged_in）原样透传。"""
    try:
        parsed = json.loads(exc.stderr or "")
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        return parsed
    return None


def _playable_recommendation_rows(rows: Any, limit: int) -> list[dict]:
    """CLI 推荐输出 -> 批量详情 + 可播过滤（未登录剔除 VIP/试听片段）。"""
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
    return batch_song_details(ids[:100])[:limit]


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
    return exec_musicbox(["auth", "login", "--check", unikey, "--json"])


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
