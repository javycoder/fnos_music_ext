"""Explicit recommendation purposes and strict runtime configuration."""
import pytest

from proxy import recommend as rec
from proxy import localrecommend as localrec


@pytest.mark.parametrize("liked,daily", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("complete", [False, True])
def test_independent_usage_gates(monkeypatch, liked, daily, complete):
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "secret" if complete else "")
    monkeypatch.setenv("FNMUSIC_LLM_LIKED_ENABLED", str(liked).lower())
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", str(daily).lower())
    assert rec.llm_liked_enabled() is (liked and complete)
    assert rec.llm_daily_enabled() is (daily and complete)


@pytest.mark.parametrize("raw,expected", [("-1", 20), ("101", 20), ("1.5", 20), ("nan", 20), ("", 20), ("0", 0), ("100", 100)])
def test_size_getter_rejects_invalid_configuration(monkeypatch, raw, expected):
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", raw)
    assert rec.llm_liked_size() == expected


@pytest.mark.parametrize("raw,expected", [("nan", .5), ("inf", .5), ("-inf", .5), ("0.4", .5), ("", .5), ("1.25", 1.25), ("10000", 10000)])
def test_hours_getter_has_no_artificial_maximum(monkeypatch, raw, expected):
    monkeypatch.setenv("FNMUSIC_LIKED_REFRESH_HOURS", raw)
    assert rec.llm_liked_refresh_hours() == expected


@pytest.mark.asyncio
async def test_selected_zero_size_stays_hidden_with_incomplete_connection(monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_LIKED_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "0")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "")
    monkeypatch.setattr(localrec, "_load_or_build", lambda *_: pytest.fail("size zero must not build local songs"))
    assert (await localrec.get_bundle("u1", "/missing.db"))["tracks"] == []


@pytest.mark.asyncio
async def test_daily_total_switch_prevents_llm_call(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "secret")
    async def forbidden(*args):
        pytest.fail("daily master switch must gate the model")
    monkeypatch.setattr(rec, "call_llm", forbidden)
    result = await rec.get_or_build_daily(
        user_guid="test-user", musicdl_client=None, musicbox_client=None,
        llm_http=object(), build_track=lambda track: track,
        netease_enabled=False, recommend_daily=False, kind="daily",
    )
    assert not result["tracks"]
