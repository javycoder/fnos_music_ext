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
@pytest.mark.parametrize("complete", [False, True])
async def test_zero_size_only_hides_enabled_llm_not_local_fallback(monkeypatch, complete):
    from proxy import app as pa
    monkeypatch.setenv("FNMUSIC_LLM_LIKED_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "0")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "secret" if complete else "")
    monkeypatch.setitem(pa.CONF, "recommend_liked", True)
    calls = []
    monkeypatch.setattr(localrec, "_load_or_build", lambda *args: calls.append(args) or {"tracks": ["local"]})
    bundle = await localrec.get_bundle("u1", "/missing.db")
    assert bundle["tracks"] == ([] if complete else ["local"])
    assert bool(calls) is not complete
    assert pa._recommend_kind_enabled("liked") is not complete


@pytest.mark.parametrize("raw", ["8761", "10000", "999999.5"])
def test_proxy_interval_parser_matches_persisted_configuration(raw):
    from proxy import app as pa
    assert pa._env_watch_parse(raw, "liked_hours") == float(raw)
    assert pa._safe_liked_number(raw, .5) == float(raw)


def test_large_liked_interval_applies_on_hot_reload(tmp_path, monkeypatch):
    from proxy import app as pa
    monkeypatch.setitem(pa.CONF, "liked_refresh_hours", .5)
    monkeypatch.setenv("FNMUSIC_LIKED_REFRESH_HOURS", ".5")
    env = tmp_path / "settings.env"
    env.write_text("FNMUSIC_LIKED_REFRESH_HOURS=10000\n")
    assert pa.apply_env_hot_reload(str(env)) == ["liked_refresh_hours"]
    assert pa.CONF["liked_refresh_hours"] == rec.llm_liked_refresh_hours() == 10000


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
