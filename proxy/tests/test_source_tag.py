"""搜索结果在线来源标记：netease→[music box]、musicdl→[dl]、lx→激活源备注名或 [lx]。

标记仅改下发显示的 title/name（搜索结果列表），本地条目与收藏/历史/元数据等
其余出口保持干净标题；播放事件上报入库前剥离标记，业务逻辑不受影响。
"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as pa
from proxy.app import (
    CONF,
    _SEARCH_CACHE,
    app,
    apply_env_hot_reload,
    build_favorite_track_obj,
    build_metadata_payload,
    build_online_track,
    source_display_prefix,
    strip_source_tag,
)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "online_favorites"))
    monkeypatch.setitem(CONF, "search_list_path", "data.list")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "netease_search_limit", 50)
    monkeypatch.setitem(CONF, "lx_search_limit", 50)
    monkeypatch.setitem(CONF, "lx_sources", [])
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "netease_wait_s", 3.0)
    monkeypatch.setitem(CONF, "merge_suggest", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "search_cache_ttl", 604800.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(CONF, "search_deep_page", True)
    monkeypatch.setitem(CONF, "search_deep_max_pages", 10)
    monkeypatch.setitem(CONF, "lx_source_url", "")
    monkeypatch.setitem(CONF, "lx_source_list", "[]")

    def quiet_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "data": []})

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(quiet_handler), base_url="http://127.0.0.1:8770"
    )


def _set_lx_source(monkeypatch, url: str, entries: list[dict]):
    monkeypatch.setitem(CONF, "lx_source_url", url)
    monkeypatch.setitem(CONF, "lx_source_list", json.dumps(entries, ensure_ascii=False))


def test_source_display_prefix_mapping(monkeypatch):
    _set_lx_source(monkeypatch, "http://s1/one.js", [
        {"name": "星海源", "url": "http://s1/one.js"},
        {"name": "", "url": "http://s2/two.js"},
    ])
    assert source_display_prefix("netease") == "[music box] "
    assert source_display_prefix("lx") == "[星海源] "
    assert source_display_prefix("kuwo") == "[dl] "
    assert source_display_prefix("migu") == "[dl] "
    assert source_display_prefix("") == ""
    # 激活源在列表里但未备注 → [lx]
    monkeypatch.setitem(CONF, "lx_source_url", "http://s2/two.js")
    assert source_display_prefix("lx") == "[lx] "
    # 激活地址不在列表（.env 手工直配）→ [lx]
    monkeypatch.setitem(CONF, "lx_source_url", "http://elsewhere/x.js")
    assert source_display_prefix("lx") == "[lx] "


def test_strip_source_tag_precise(monkeypatch):
    _set_lx_source(monkeypatch, "http://s1/one.js", [{"name": "星海源", "url": "http://s1/one.js"}])
    assert strip_source_tag("[music box] 晴天") == "晴天"
    assert strip_source_tag("[dl] 晴天") == "晴天"
    assert strip_source_tag("[lx] 晴天") == "晴天"
    # 列表内全部备注名都可剥离（含未激活源，历史标记切换源后仍可还原）
    assert strip_source_tag("[星海源] 夜曲") == "夜曲"
    # 未知方括号开头的内容不是来源标记，不剥离
    assert strip_source_tag("[AI生成] 晴天") == "[AI生成] 晴天"
    assert strip_source_tag("晴天") == "晴天"
    assert strip_source_tag("") == ""


def test_build_online_track_mark_source_only_title():
    item = {
        "id": "netease:123",
        "source": "netease",
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "duration_s": 269,
        "ext": "flac",
    }
    clean = build_online_track(item)
    assert clean["title"] == "晴天"
    assert clean["name"] == "晴天"

    marked = build_online_track(item, mark_source=True)
    assert marked["title"] == "[music box] 晴天"
    assert marked["name"] == "[music box] 晴天"
    # 标记只加在歌名前，歌手/专辑不动
    assert marked["artist"] == "周杰伦"
    assert marked["album"]["name"] == "叶惠美"

    # 空标题不加标记
    empty = build_online_track(
        {"id": "migu:1", "source": "migu", "title": "", "artist": "x"}, mark_source=True
    )
    assert empty["title"] == ""


def test_search_merge_marks_online_not_local():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [{
                    "guid": "local:101",
                    "title": "夜曲",
                    "artist": "周杰伦",
                    "album": "十一月的萧邦",
                    "duration": 226000,
                }],
                "total": 1,
            },
        })

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": [{
            "id": "migu:600902",
            "source": "migu",
            "title": "晴天",
            "artist": "周杰伦",
            "album": "叶惠美",
            "duration_s": 269,
            "ext": "mp3",
            "file_size": 4096000,
        }]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        lst = resp.json()["data"]["list"]
    titles = [it["title"] for it in lst]
    assert titles[0] == "夜曲"
    assert "[dl] 晴天" in titles
    # 同名本地条目存在时在线去重（按干净标题），不会出现带标记的重复
    assert titles.count("晴天") == 0


def test_search_merge_lx_remark_and_fallback(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    _set_lx_source(monkeypatch, "http://s1/one.js", [{"name": "星海源", "url": "http://s1/one.js"}])

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "OK", "data": {"list": [], "total": 0}})

    def lx_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": [{
            "id": "lx:kg:ABC123",
            "lx_source": "kg",
            "title": "晴天",
            "artist": "周杰伦",
            "album": "叶惠美",
            "duration_s": 269,
            "ext": "flac",
            "file_size": 28000000,
        }]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        titles = [it["title"] for it in resp.json()["data"]["list"]]
        assert titles == ["[星海源] 晴天"]

        # 切换到无备注激活源：清缓存重搜 → [lx] 兜底
        monkeypatch.setitem(CONF, "lx_source_url", "")
        _SEARCH_CACHE.clear()
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        titles = [it["title"] for it in resp.json()["data"]["list"]]
        assert titles == ["[lx] 晴天"]


def test_event_report_strips_source_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    _set_lx_source(monkeypatch, "http://s1/one.js", [{"name": "星海源", "url": "http://s1/one.js"}])

    def auth_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-tag-1"}})
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(auth_handler), base_url="http://unix"
    )
    with TestClient(app) as client:
        resp = client.post("/music/api/v1/event/report", json={"events": [
            {"eventType": "track_play", "occurredAt": 1,
             "payload": {"trackGUID": "online:migu:1", "title": "[dl] 晴天", "artist": "周杰伦"}},
            {"eventType": "track_play", "occurredAt": 2,
             "payload": {"trackGUID": "online:lx:kg:9", "title": "[星海源] 夜曲", "artist": "周杰伦"}},
            {"eventType": "track_play", "occurredAt": 3,
             "payload": {"trackGUID": "online:netease:5", "title": "[music box] 七里香", "artist": "周杰伦"}},
        ]})
        assert resp.json()["code"] == 0

    from proxy import recommend as dailyrec

    items = dailyrec.load_online_play_history("user-tag-1")
    by_guid = {it["guid"]: it for it in items}
    assert by_guid["online:migu:1"]["track"]["title"] == "晴天"
    assert by_guid["online:lx:kg:9"]["track"]["title"] == "夜曲"
    assert by_guid["online:netease:5"]["track"]["title"] == "七里香"


def test_env_hot_reload_lx_source(tmp_path, monkeypatch):
    # 预登记环境变量，让 monkeypatch 在收尾时恢复原值（apply_env_hot_reload 会直写 os.environ）
    monkeypatch.setenv("LX_SOURCE_URL", "")
    monkeypatch.setenv("LX_SOURCE_LIST", "[]")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LX_SOURCE_URL=http://s1/one.js\n"
        "LX_SOURCE_LIST='[{\"name\": \"星海源\", \"url\": \"http://s1/one.js\"}]'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pa, "CONF", dict(CONF))
    apply_env_hot_reload(str(env_file))
    assert pa.CONF["lx_source_url"] == "http://s1/one.js"
    assert json.loads(pa.CONF["lx_source_list"])[0]["name"] == "星海源"
    assert source_display_prefix("lx") == "[星海源] "
    assert strip_source_tag("[星海源] 晴天") == "晴天"


def test_favorite_and_metadata_vo_not_marked():
    guid = "online:netease:123"
    info = {"title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269}
    fav = build_favorite_track_obj(guid, info=info)
    assert fav["title"] == "晴天"

    meta = build_metadata_payload(guid, info)
    assert meta["data"]["track"]["title"] == "晴天"
