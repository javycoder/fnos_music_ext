"""切换保存目录后的过期 .ref 词干治理回归测试（issue #88 后续加固）。

场景：用户改过保存目录（tee_save_dir / 曲库目录）后，cache 里残留指向旧目录的
.ref。修复前 library_media_path 会沿旧词干返回旧目录路径——实际写入目标与
staged/direct 判定基准（当前配置目录）脱钩，远程目录重新出现 .part 与改名。
"""
import importlib
import os

import pytest

p = importlib.import_module("proxy.app")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("cache_dir", "library_dir", "fav_dir"):
        monkeypatch.setitem(p.CONF, key, str(tmp_path / key))
    monkeypatch.setitem(p.CONF, "music_db", str(tmp_path / "missing.db"))
    monkeypatch.setitem(p.CONF, "tee_save_enabled", True)
    monkeypatch.setitem(p.CONF, "tee_save_dir", "")
    monkeypatch.setitem(p.CONF, "tee_save_mode", "auto")
    monkeypatch.setitem(p.CONF, "download_layout", "flat")
    monkeypatch.setenv("FNMUSIC_TEE_SAVE_MODE", "auto")
    monkeypatch.setattr(p, "_TEE_STAGED_CACHE", {})
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", False)
    monkeypatch.setattr(p, "_TEE_SAVE_DIR_WARNED", False)
    os.makedirs(p.CONF["cache_dir"], exist_ok=True)
    os.makedirs(p.CONF["library_dir"], exist_ok=True)


@pytest.fixture(autouse=True)
def _portable_layout_dirs(monkeypatch):
    """organize._directory 依赖 POSIX os.O_DIRECTORY；Windows 本地跑法替换为
    纯 makedirs，Linux CI 仍走真实实现（符号链接检查在 CI 侧覆盖）。"""
    if not hasattr(os, "O_DIRECTORY"):
        monkeypatch.setattr(
            p, "ensure_layout_dir",
            lambda root, target, create_root=False: os.makedirs(target, exist_ok=True),
        )


def _install(part_path, guid, ext="flac", title="晴天", artist="周杰伦",
             album="叶惠美"):
    return p._install_library_audio(part_path, guid, ext, title, artist, album, {})


def test_stale_ref_outside_current_roots_redirects_to_new_dir(tmp_path, monkeypatch):
    """幽灵词干改判：旧目录文件已不存在且词干不在当前保存根内 → 回落当前
    保存根正常命名，旧目录零写入（含 .part/.lrc），引用更新到新位置。"""
    old_root = tmp_path / "old-remote"
    new_root = tmp_path / "library"
    old_root.mkdir()
    new_root.mkdir()
    monkeypatch.setitem(p.CONF, "library_dir", str(new_root))
    monkeypatch.setitem(p.CONF, "tee_save_dir", str(new_root))
    guid = "online:kuwo:stale-ref"
    p.remember_media_path(guid, str(old_root / "周杰伦 - 晴天"))

    part = tmp_path / "download.part"
    part.write_bytes(b"A" * 4096)
    dest, reused = _install(str(part), guid, ext="mp3")

    assert dest == os.path.join(str(new_root), "周杰伦 - 晴天.mp3")
    assert reused is False
    with open(dest, "rb") as f:
        assert f.read() == b"A" * 4096
    # 旧目录必须零新增（音频、.part、.lrc 什么都不许出现）
    assert os.listdir(str(old_root)) == []
    assert p.recalled_media_path(guid) == dest
    assert not part.exists()


def test_staged_decision_follows_actual_dest_dir(tmp_path, monkeypatch):
    """判定跟随实际落点：tee_save_dir 是本地目录，但词干仍指向当前曲库根内
    且该目录判定为远程 → 必须走 staged 单次上传，目标目录旁不得出现 .part。"""
    lib_root = tmp_path / "lib"
    local_save = tmp_path / "local-save"
    lib_root.mkdir()
    local_save.mkdir()
    monkeypatch.setitem(p.CONF, "library_dir", str(lib_root))
    monkeypatch.setitem(p.CONF, "tee_save_dir", str(local_save))
    remote_dirs = {os.path.realpath(str(lib_root))}
    monkeypatch.setattr(
        p, "_should_stage",
        lambda target, cache_dir, mode: (os.path.realpath(target) in remote_dirs, "test"),
    )
    guid = "online:kuwo:remote-stem"
    # 词干文件不存在，但目录仍在当前根（曲库根）内 → 沿用旧词干定名
    p.remember_media_path(guid, str(lib_root / "周杰伦 - 晴天"))

    part = tmp_path / "download.part"
    part.write_bytes(b"R" * 4096)
    dest, reused = _install(str(part), guid, ext="flac")

    assert dest == os.path.join(str(lib_root), "周杰伦 - 晴天.flac")
    assert reused is False
    with open(dest, "rb") as f:
        assert f.read() == b"R" * 4096
    # staged 路径证据：暂存区加工完即清空，目标目录无 .part 残件
    assert os.listdir(p.staged_dir()) == []
    assert not any(name.endswith(".part") for name in os.listdir(str(lib_root)))
    assert p.recalled_media_path(guid) == dest


def test_existing_file_in_abandoned_dir_still_reused(tmp_path, monkeypatch):
    """复用语义不回归（issue #52）：旧引用指向的文件仍存在时，即使已切换
    保存目录也照旧复用——字节与 mtime 绝不动，本次下载直接丢弃。"""
    old_root = tmp_path / "old-dir"
    new_root = tmp_path / "library"
    old_root.mkdir()
    new_root.mkdir()
    existing = old_root / "周杰伦 - 晴天.flac"
    existing.write_bytes(b"USER-DATA")
    monkeypatch.setitem(p.CONF, "library_dir", str(new_root))
    monkeypatch.setitem(p.CONF, "tee_save_dir", str(new_root))
    guid = "online:netease:keep-old"
    p.remember_media_path(guid, str(old_root / "周杰伦 - 晴天"))
    mtime_before = existing.stat().st_mtime_ns

    part = tmp_path / "download.part"
    part.write_bytes(b"A" * 4096)
    dest, reused = _install(str(part), guid, ext="flac")

    assert dest == str(existing)
    assert reused is True
    assert existing.read_bytes() == b"USER-DATA"
    assert existing.stat().st_mtime_ns == mtime_before
    assert not part.exists()
    assert p.recalled_media_path(guid) == str(existing)


def test_quality_change_reuses_sibling_in_current_root(tmp_path):
    """词干仍在当前根内时不改判：同词干已有其他扩展名（换音质重落盘）
    继续复用，不制造第二份同曲文件。"""
    lib_root = p.CONF["library_dir"]
    sibling = os.path.join(lib_root, "周杰伦 - 晴天.flac")
    with open(sibling, "wb") as f:
        f.write(b"OLD-FLAC")
    guid = "online:netease:quality"
    p.remember_media_path(guid, os.path.join(lib_root, "周杰伦 - 晴天"))

    part = os.path.join(p.CONF["cache_dir"], "quality.part")
    with open(part, "wb") as f:
        f.write(b"A" * 4096)
    dest, reused = _install(part, guid, ext="mp3")

    assert dest == sibling
    assert reused is True
    with open(sibling, "rb") as f:
        assert f.read() == b"OLD-FLAC"
    # 曲库仍只有这一个音频文件
    assert sorted(os.listdir(lib_root)) == ["周杰伦 - 晴天.flac"]


def test_shadow_lyric_promotion_is_staged_for_remote_dest(tmp_path, monkeypatch):
    """影子歌词提升分流：落点目录按显式 staged 配置判为远程 → 歌词走本地
    成文单次写入，目标目录无 .part、暂存区无残留、影子副本清除。"""
    monkeypatch.setitem(p.CONF, "tee_save_mode", "staged")
    monkeypatch.setenv("FNMUSIC_TEE_SAVE_MODE", "staged")
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    lib_root = p.CONF["library_dir"]
    guid = "online:kuwo:shadow"
    dest = os.path.join(lib_root, "周杰伦 - 晴天.mp3")
    with open(dest, "wb") as f:
        f.write(b"A" * 64)
    p.remember_media_path(guid, dest)
    shadow = os.path.join(p.CONF["cache_dir"], f"{p.cache_safe_guid(guid)}.lrc")
    with open(shadow, "w", encoding="utf-8") as f:
        f.write("[00:00.00]影子歌词")

    part = os.path.join(p.CONF["cache_dir"], "shadow.part")
    with open(part, "wb") as f:
        f.write(b"A" * 64)
    got, reused = _install(part, guid, ext="mp3")

    assert got == dest
    assert reused is True
    lrc = os.path.splitext(dest)[0] + ".lrc"
    with open(lrc, encoding="utf-8") as f:
        assert "影子歌词" in f.read()
    assert not os.path.exists(shadow)
    assert not any(name.endswith(".part") for name in os.listdir(lib_root))
    assert os.listdir(p.staged_dir()) == []


def test_shadow_lyric_tolerates_non_utf8_and_cleans_empty_shadow(tmp_path, monkeypatch):
    """影子歌词包含非 UTF-8 字节时不抛 UnicodeDecodeError，空白影子文件自动清理。"""
    monkeypatch.setitem(p.CONF, "tee_save_mode", "direct")
    monkeypatch.setenv("FNMUSIC_TEE_SAVE_MODE", "direct")
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    lib_root = p.CONF["library_dir"]

    # 1) 非 UTF-8 字节影子歌词不得中断音频落盘
    guid1 = "online:kuwo:gbk-shadow"
    shadow1 = os.path.join(p.CONF["cache_dir"], f"{p.cache_safe_guid(guid1)}.lrc")
    with open(shadow1, "wb") as f:
        f.write(b"[00:00.00]\xc7\xe7\xcc\xec")  # GBK 字节
    part1 = os.path.join(p.CONF["cache_dir"], "gbk.part")
    with open(part1, "wb") as f:
        f.write(b"A" * 128)
    dest1, reused1 = _install(part1, guid1, ext="mp3")
    assert reused1 is False
    assert os.path.exists(dest1)
    assert os.path.exists(os.path.splitext(dest1)[0] + ".lrc")
    assert not os.path.exists(shadow1)

    # 2) 空白影子歌词不生成空 sidecar 且清理残留 shadow
    guid2 = "online:kuwo:empty-shadow"
    shadow2 = os.path.join(p.CONF["cache_dir"], f"{p.cache_safe_guid(guid2)}.lrc")
    with open(shadow2, "w", encoding="utf-8") as f:
        f.write("   \n  ")
    part2 = os.path.join(p.CONF["cache_dir"], "empty.part")
    with open(part2, "wb") as f:
        f.write(b"B" * 128)
    dest2, reused2 = _install(part2, guid2, ext="mp3", title="江南", artist="林俊杰")
    assert reused2 is False
    assert os.path.exists(dest2)
    assert not os.path.exists(os.path.splitext(dest2)[0] + ".lrc")
    assert not os.path.exists(shadow2)


def test_direct_mode_recreates_missing_subdir_and_ignores_cache_root_stem(tmp_path, monkeypatch):
    """同根旧词干的子目录已被删时 direct 模式自动补建父目录；cache_dir 内的非滚动词干不视作保存根。"""
    monkeypatch.setitem(p.CONF, "tee_save_mode", "direct")
    monkeypatch.setenv("FNMUSIC_TEE_SAVE_MODE", "direct")
    lib_root = p.CONF["library_dir"]

    # cache_dir 内的非滚动词干（文件已不存在）不应被视作曲库保存根，应回落到 library_dir
    guid_cache = "online:kuwo:cache-stem"
    p.remember_media_path(guid_cache, os.path.join(p.CONF["cache_dir"], "orphan", "周杰伦 - 晴天"))
    part_a = tmp_path / "a.part"
    part_a.write_bytes(b"A" * 512)
    dest_a, _ = _install(str(part_a), guid_cache, ext="mp3")
    assert dest_a == os.path.join(lib_root, "周杰伦 - 晴天.mp3")

    # 曲库根内的旧词干子目录已被用户删除 → direct 落盘应自动补建父目录而非抛 FileNotFoundError
    guid_sub = "online:kuwo:missing-sub"
    missing_sub = os.path.join(lib_root, "deleted-artist", "deleted-album")
    p.remember_media_path(guid_sub, os.path.join(missing_sub, "林俊杰 - 江南"))
    part_b = tmp_path / "b.part"
    part_b.write_bytes(b"B" * 512)
    dest_b, _ = _install(str(part_b), guid_sub, ext="mp3", title="江南", artist="林俊杰")
    assert dest_b == os.path.join(missing_sub, "林俊杰 - 江南.mp3")
    assert os.path.exists(dest_b)

