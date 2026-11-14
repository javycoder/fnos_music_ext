import json
import os
import shutil
import subprocess
import time
from pathlib import Path
import pytest

from proxy.netease_legacy import (
    import_legacy_cookie,
    is_netease_provider,
    migrate_env_config,
    migrate_session,
    normalize_provider,
)


def test_provider_normalization():
    assert normalize_provider("musicbox") == "netease"
    assert normalize_provider("netease") == "netease"
    assert normalize_provider("lx") == "lx"
    assert is_netease_provider("musicbox") is True
    assert is_netease_provider("netease") is True
    assert is_netease_provider("lx") is False


def test_env_config_migration_idempotent_and_new_priority(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FNMUSIC_MUSICBOX_URL=http://127.0.0.1:8770\n"
        "FNMUSIC_MUSICBOX_ENABLED=true\n"
        "CUSTOM_KEY=keep_me\n",
        encoding="utf-8",
    )

    # First migration: maps MUSICBOX keys to NETEASE keys
    changed = migrate_env_config(env_file)
    assert changed is True

    content = env_file.read_text(encoding="utf-8")
    assert "FNMUSIC_NETEASE_URL='http://127.0.0.1:8770'" in content or "FNMUSIC_NETEASE_URL=http://127.0.0.1:8770" in content
    assert "FNMUSIC_NETEASE_ENABLED='true'" in content or "FNMUSIC_NETEASE_ENABLED=true" in content
    assert "CUSTOM_KEY='keep_me'" in content or "CUSTOM_KEY=keep_me" in content

    # Second migration: already migrated, no changes
    changed2 = migrate_env_config(env_file)
    assert changed2 is False

    # New key has priority
    env_file.write_text(
        "FNMUSIC_MUSICBOX_URL=http://old.url:8770\n"
        "FNMUSIC_NETEASE_URL=http://new.url:8770\n",
        encoding="utf-8",
    )
    migrate_env_config(env_file)
    content2 = env_file.read_text(encoding="utf-8")
    assert "FNMUSIC_NETEASE_URL='http://new.url:8770'" in content2 or "FNMUSIC_NETEASE_URL=http://new.url:8770" in content2


def test_mozilla_cookie_jar_import_and_expiration_filtering(tmp_path):
    data_dir = tmp_path / "sources-data"
    mb_dir = data_dir / "netease-musicbox"
    mb_dir.mkdir(parents=True)
    cookie_file = mb_dir / "cookie.txt"

    now = int(time.time())
    future = now + 100000
    past = now - 100000

    # Write a Netscape/Mozilla format cookie file
    cookie_content = (
        "# Netscape HTTP Cookie File\n"
        f".music.163.com\tTRUE\t/\tTRUE\t{future}\tMUSIC_U\tvalid_user_token\n"
        f".music.163.com\tTRUE\t/\tFALSE\t{future}\t__csrf\tvalid_csrf\n"
        f".music.163.com\tTRUE\t/\tFALSE\t{past}\tEXPIRED\texpired_value\n"
        f".example.com\tTRUE\t/\tFALSE\t{future}\tOTHER\tother_value\n"
    )
    cookie_file.write_text(cookie_content, encoding="utf-8")

    imported = import_legacy_cookie(data_dir)
    assert imported is not None
    cookies = imported["cookies"]
    assert cookies["MUSIC_U"] == "valid_user_token"
    assert cookies["__csrf"] == "valid_csrf"
    # Expired and other domain cookies must be filtered out
    assert "EXPIRED" not in cookies
    assert "OTHER" not in cookies


def test_migrate_session_atomicity_and_existing_priority(tmp_path):
    data_dir = tmp_path / "sources-data"
    mb_dir = data_dir / "netease-musicbox"
    mb_dir.mkdir(parents=True)
    cookie_file = mb_dir / "cookie.txt"

    future = int(time.time()) + 100000
    cookie_file.write_text(
        f".music.163.com\tTRUE\t/\tTRUE\t{future}\tMUSIC_U\told_token\n",
        encoding="utf-8",
    )

    # 1. Initial migration
    assert migrate_session(data_dir) is True
    session_file = data_dir / "netease" / "session.json"
    assert session_file.is_file()
    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["cookies"]["MUSIC_U"] == "old_token"
    assert session_file.stat().st_mode & 0o777 == 0o600
    assert (data_dir / "netease").stat().st_mode & 0o777 == 0o700

    # 2. Existing session priority: if session has new token, migrate does not overwrite with old token
    data["cookies"]["MUSIC_U"] = "newer_token"
    data["cookie_str"] = "MUSIC_U=newer_token"
    session_file.write_text(json.dumps(data), encoding="utf-8")

    assert migrate_session(data_dir) is True
    data_after = json.loads(session_file.read_text(encoding="utf-8"))
    assert data_after["cookies"]["MUSIC_U"] == "newer_token"

    # 3. Old cookie file is kept for backup/recovery
    assert cookie_file.is_file()


def test_corrupted_cookie_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "sources-data"
    mb_dir = data_dir / "netease-musicbox"
    mb_dir.mkdir(parents=True)
    cookie_file = mb_dir / "cookie.txt"
    cookie_file.write_text("corrupted garbage content not a cookie file\n", encoding="utf-8")

    imported = import_legacy_cookie(data_dir)
    assert imported is None
    assert migrate_session(data_dir) is False


@pytest.mark.parametrize("first_cookie", ["corrupt", "expired"])
def test_legacy_import_tries_other_paths_if_first_cookie_is_unusable(
    tmp_path, monkeypatch, first_cookie,
):
    data_dir = tmp_path / "sources-data"
    primary = data_dir / "netease-musicbox"
    secondary = data_dir / "config" / "netease-musicbox"
    primary.mkdir(parents=True)
    secondary.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
    past = int(time.time()) - 100
    future = int(time.time()) + 100000
    first = ("not a cookie jar\n" if first_cookie == "corrupt" else
             f"# Netscape HTTP Cookie File\n.music.163.com\tTRUE\t/\tFALSE\t{past}\tMUSIC_U\texpired\n")
    (primary / "cookie.txt").write_text(first)
    (secondary / "cookie.txt").write_text(
        f"# Netscape HTTP Cookie File\n.music.163.com\tTRUE\t/\tFALSE\t{future}\tMUSIC_U\tvalid\n"
    )

    assert migrate_session(data_dir)
    session = json.loads((data_dir / "netease" / "session.json").read_text())
    assert session["cookies"]["MUSIC_U"] == "valid"


def test_migration_keeps_env_comments_and_rejects_unrelated_cookie_domains(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# important setting\nFNMUSIC_MUSICBOX_ENABLED=true\n", encoding="utf-8")
    assert migrate_env_config(env_file)
    assert "# important setting\n" in env_file.read_text(encoding="utf-8")

    cookie_dir = tmp_path / "netease-musicbox"
    cookie_dir.mkdir()
    future = int(time.time()) + 100000
    (cookie_dir / "cookie.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        f".music.163.com\tTRUE\t/\tFALSE\t{future}\tMUSIC_U\tvalid\n"
        f".evil163.com\tTRUE\t/\tFALSE\t{future}\tOTHER\trejected\n",
        encoding="utf-8",
    )
    imported = import_legacy_cookie(tmp_path)
    assert imported is not None
    assert imported["cookies"] == {"MUSIC_U": "valid"}


def test_node_entrypoint_migrates_existing_legacy_session(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    root = Path(__file__).resolve().parents[2]
    data_dir = tmp_path / "data"
    cookie_dir = data_dir / "netease-musicbox"
    cookie_dir.mkdir(parents=True)
    future = int(time.time()) + 100000
    (cookie_dir / "cookie.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        f".music.163.com\tTRUE\t/\tFALSE\t{future}\tMUSIC_U\tlegacy_token\n",
        encoding="utf-8",
    )
    script = """
const Module = require('module');
const original = Module._load;
Module._load = function(name, parent, main) {
  if (name === './service' && parent.filename.endsWith('/netease-service/app.js')) {
    return { createService: () => ({
      app: { listen: () => ({ close: done => done() }) },
      ensureInitialized: async () => true,
    }) };
  }
  return original.apply(this, arguments);
};
require('./netease-service/app.js');
"""
    env = os.environ.copy()
    env.update(SOURCES_DATA_DIR=str(data_dir), HOME=str(tmp_path))
    env.pop("NETEASE_SESSION_FILE", None)
    result = subprocess.run([node, "-e", script], cwd=root, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    session_file = data_dir / "netease" / "session.json"
    assert json.loads(session_file.read_text())["cookies"]["MUSIC_U"] == "legacy_token"
    assert session_file.stat().st_mode & 0o777 == 0o600
