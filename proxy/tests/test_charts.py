"""38 榜单离线测试：GUID 避让、开关过滤、缓存回退、按天重建、统一失效。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import proxy.charts as charts


def test_guid_prefix_avoids_hot():
    assert charts.CHART_GUID_PREFIX == "online:playlist:chart:"
    assert charts.CHART_GUID_PREFIX != "online:playlist:hot:"
    assert charts.is_chart_guid(charts.chart_guid("kg_8888"))
    assert charts.chart_id_from_guid(charts.chart_guid("kg_8888")) == "kg_8888"


def test_list_enabled_charts_switches(tmp_path, monkeypatch):
    assert charts.list_enabled_charts(enable_charts=False) == []
    kg_only = charts.list_enabled_charts(True, True, False)
    assert kg_only and all(c["source"] == "kg" for c in kg_only)
    wy_only = charts.list_enabled_charts(True, False, True)
    assert wy_only and all(c["source"] == "wy" for c in wy_only)
    wl = charts.list_enabled_charts(True, True, True, ["kg_8888"])
    assert [c["id"] for c in wl] == ["kg_8888"]


def test_cache_fallback_to_history(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path))
    charts._MEM_CACHE.clear()
    charts.save_chart_cache("kg_8888", "20260101", [{"id": "lx:kg:abc"}], cover_url="http://x/c.jpg")
    # 当天缺失时回落最近历史
    got = charts.load_chart_cache("kg_8888", "20261231")
    assert got and got[0]["id"] == "lx:kg:abc"


def test_history_keeps_three(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path))
    charts._MEM_CACHE.clear()
    for day in ["20260101", "20260102", "20260103", "20260104", "20260105"]:
        charts.save_chart_cache("kg_6666", day, [{"id": "x"}])
    cdir = charts._cache_dir()
    files = sorted(f for f in os.listdir(cdir) if f.startswith("kg_6666_"))
    assert len(files) == 3


def test_purge_chart_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path))
    charts._MEM_CACHE.clear()
    charts.save_chart_cache("kg_8888", "20260101", [{"id": "x"}])
    assert charts.purge_chart_caches() >= 1
    assert charts._MEM_CACHE == {}
