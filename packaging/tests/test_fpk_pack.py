"""fpk 打包结构离线测试。

通过 build.sh --stage-only 组装打包目录后校验：
- manifest 必填字段、版本与仓库 VERSION 一致、入口名以应用名为前缀
- ui/config 桌面入口（iframe + 8774）与 manifest 的 desktop_applaunchname 一致
- fnpack 逆向校验规则：wizard initValue 必须是字符串、tips 用 helpText
- 图标存在且尺寸正确（纯 struct 解析 PNG IHDR，无 PIL 依赖）
- cmd 生命周期脚本存在且可执行
- 组装目录不泄漏开发文件与本地秘密（.env/.git/tests/docs/packaging）

仅依赖 bash/rsync，无需 fnpack 与飞牛环境（CI 全平台可跑）。
"""

import json
import hashlib
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FPK_DIR = REPO_ROOT / "packaging" / "fpk"
APPNAME = "fnmusic-ext"
ENTRY_ID = "fnmusic-ext.main"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} 不是 PNG 文件"
    assert data[12:16] == b"IHDR", f"{path} 缺少 IHDR 段"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


@pytest.fixture(scope="module")
def stage(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("fpk-stage")
    subprocess.run(
        ["bash", str(FPK_DIR / "build.sh"), "--stage-only", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def _parse_manifest(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"manifest 行缺少 =: {line}"
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


class TestManifest:
    def test_required_fields(self, stage: Path):
        assert json.loads((stage / "app/repo/FNMUSIC_COMPAT.json").read_text(encoding="utf-8"))["extension_version"] == (
            REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        m = _parse_manifest((stage / "manifest").read_text(encoding="utf-8"))
        assert m["appname"] == APPNAME
        assert m["source"] == "thirdparty"
        assert m["platform"] == "all"
        assert m["desktop_uidir"] == "ui"
        assert m["desktop_applaunchname"] == ENTRY_ID
        assert "trim.music" in m["install_dep_apps"]
        assert m["checkport"] == "false"
        assert m["maintainer_url"].startswith("https://github.com/")
        # JS SDK（pickUserFile NAS 文件选择）要求微应用环境
        assert m["micro_app"] == "true"

    def test_version_matches_repo(self, stage: Path):
        m = _parse_manifest((stage / "manifest").read_text(encoding="utf-8"))
        repo_version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        assert re.fullmatch(r"\d+\.\d+\.\d+(?:-\d+)?", repo_version), \
            f"VERSION 必须是 x.y.z 或 x.y.z-N，当前为 {repo_version!r}"
        assert m["version"] == repo_version
        assert (stage / "app/repo/VERSION").read_text(encoding="utf-8").strip() == repo_version
        assert (stage / "app/repo/RELEASE_CHANNEL").read_text().strip() == (
            REPO_ROOT / "RELEASE_CHANNEL").read_text().strip()

    def test_template_has_placeholder(self):
        assert "@VERSION@" in (FPK_DIR / "manifest.in").read_text(encoding="utf-8")


class TestUiConfig:
    def test_entry(self, stage: Path):
        cfg = json.loads((stage / "app" / "ui" / "config").read_text(encoding="utf-8"))
        entry = cfg[".url"][ENTRY_ID]
        assert entry["type"] == "iframe"
        # 空端口 + 网关路径：桌面是 HTTPS，不能把管理页嵌成 http://主机:8774
        assert entry["port"] == ""
        assert entry["protocol"] == "http"
        assert entry["url"] == "/app/fnmusic-ext/"
        assert entry["gatewayPrefix"] == "/app/fnmusic-ext"
        assert entry["gatewaySocket"] == "fnmusic-ext.sock"
        assert entry["allUsers"] is False
        assert entry["icon"] == "images/icon_{0}.png"
        # fnpack 强制入口名以应用名开头（实测规则）
        assert ENTRY_ID.startswith(APPNAME + ".")


class TestConfig:
    def test_privilege_run_as_root(self, stage: Path):
        priv = json.loads((stage / "config" / "privilege").read_text(encoding="utf-8"))
        assert priv["defaults"]["run-as"] == "root"

    def test_resource_is_json(self, stage: Path):
        resource = json.loads((stage / "config" / "resource").read_text(encoding="utf-8"))
        # NAS 文件选择（pickUserFile）需要声明的开放 API scope
        assert resource.get("api-scope") == ["trim.file.userAccess"]


class TestWizard:
    def test_install_wizard(self, stage: Path):
        steps = json.loads((stage / "wizard" / "install").read_text(encoding="utf-8"))
        assert steps, "install 向导不能为空"
        items = steps[0]["items"]
        fields = {it["field"] for it in items if "field" in it}
        assert "wizard_sources" in fields
        assert "wizard_extend" in fields
        assert "wizard_webui_password" not in fields
        radio = next(it for it in items if it["type"] == "radio")
        values = {opt["value"] for opt in radio["options"]}
        assert values == {"musicdl", "musicbox", "lxmusic"}

    def test_fnpack_quirks(self, stage: Path):
        """fnpack 实测逆向出的两条硬规则（布尔 initValue / tips 用 text 会打包失败）。"""
        for name in ("install",):
            steps = json.loads((stage / "wizard" / name).read_text(encoding="utf-8"))
            for step in steps:
                for it in step["items"]:
                    if "initValue" in it:
                        assert isinstance(it["initValue"], str), (
                            f"wizard/{name}: initValue 必须是字符串（fnpack 拒绝布尔值）"
                        )
                    if it["type"] == "tips":
                        assert "helpText" in it and "text" not in it, (
                            f"wizard/{name}: tips 必须用 helpText 而不是 text"
                        )

    def test_no_uninstall_wizard(self, stage: Path):
        """CLI 卸载会被带输入项的 uninstall 向导阻塞（实测），向导目录只允许 install。"""
        names = {p.name for p in (stage / "wizard").iterdir() if p.is_file()}
        assert names == {"install"}, f"wizard/ 只应包含 install，实际: {names}"

    def test_field_names_prefixed(self, stage: Path):
        for name in ("install",):
            steps = json.loads((stage / "wizard" / name).read_text(encoding="utf-8"))
            for st in steps:
                for it in st["items"]:
                    if "field" in it:
                        assert it["field"].startswith("wizard_"), (
                            f"wizard/{name}: 字段 {it['field']} 需要 wizard_ 前缀"
                        )


class TestIcons:
    @pytest.mark.parametrize(
        "rel,size",
        [
            ("ICON.PNG", 64),
            ("ICON_256.PNG", 256),
            ("app/ui/images/icon_64.png", 64),
            ("app/ui/images/icon_256.png", 256),
        ],
    )
    def test_size(self, stage: Path, rel: str, size: int):
        path = stage / rel
        assert path.is_file(), f"缺少 {rel}"
        assert _png_size(path) == (size, size), f"{rel} 尺寸应为 {size}x{size}"
        assert path.stat().st_size <= 1024 * 1024


class TestCmdScripts:
    def test_all_scripts_present_and_executable(self, stage: Path):
        names = [
            "main", "install_init", "install_callback",
            "upgrade_init", "upgrade_callback",
            "uninstall_init", "uninstall_callback",
            "config_init", "config_callback",
        ]
        for name in names:
            path = stage / "cmd" / name
            assert path.is_file(), f"缺少 cmd/{name}"
            assert os.access(path, os.X_OK), f"cmd/{name} 缺少可执行权限"

    # Lifecycle semantics are exercised by test_fpk_cmds.py against real shell
    # scripts in a sandbox. Checking that keyword strings exist here duplicated
    # that coverage without detecting broken execution or argument forwarding.


class TestPayload:
    def test_logs_and_exports_excluded_by_real_rsync(self, tmp_path):
        # Execute the exact production rsync stanza against a miniature checkout,
        # without adding fixture secrets/logs to the actual working tree.
        source = tmp_path / "source"
        source.mkdir()
        paths = (
            "logs/proxy.log", "logs/proxy.log.2026-10-09",
            "logs/env_snapshot.txt", "logs/exports/support.logzip",
            "logs/other/nested.bin", "export.logzip", "support/export.logzip",
            "proxy/debug.log", "keep.py", ".env.example",
        )
        for rel in paths:
            path = source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        stage = tmp_path / "stage"
        target = stage / "app" / "repo"
        target.mkdir(parents=True)
        text = (FPK_DIR / "build.sh").read_text(encoding="utf-8")
        start = text.index("rsync -a --delete")
        end = text.index('"${REPO_ROOT}/" "${STAGE}/app/repo/"', start)
        block = text[start:end] + '"${REPO_ROOT}/" "${STAGE}/app/repo/"'
        subprocess.run(["bash", "-c", block], check=True, capture_output=True,
                       env={**os.environ, "REPO_ROOT": str(source), "STAGE": str(stage)})
        assert not (target / "logs").exists()
        assert not list(target.rglob("*.logzip"))
        assert not list(target.rglob("*.log"))
        assert (target / "keep.py").is_file()
        assert (target / ".env.example").is_file()
        # The root logs entry is excluded even when it is a symlink.
        shutil.rmtree(source / "logs")
        (source / "logs").symlink_to(source / "support", target_is_directory=True)
        subprocess.run(["bash", "-c", block], check=True, capture_output=True,
                       env={**os.environ, "REPO_ROOT": str(source), "STAGE": str(stage)})
        assert not (target / "logs").is_symlink()

    def test_dev_files_and_runtime_maps_excluded_by_real_rsync(self, tmp_path):
        source = tmp_path / "source"
        stage = tmp_path / "stage"
        target = stage / "app/repo"
        target.mkdir(parents=True)
        excluded = (
            "test_root.py", "test_root.js", "conftest.py", "smoke_test.sh",
            "scripts/run_tests.py",
            "proxy/test_loose.py", "webui-service/test_app_js.js",
            "nested/conftest.py", "nested/smoke_test.sh",
            ".agents/skills/local/SKILL.md", "nested/.agents/config.json",
            "AGENTS.md", "nested/AGENTS.md",
            "playlist_tracks/user.json", "nested/playlist_tracks/user.json",
            "album_registry.json", "bind_registry.json", "online_favorites.json",
            "nested/album_registry.json", "nested/bind_registry.json",
            "online_favorites/user.json", "cache/track.mp3",
            "sources-data/session.json", "play_history/user.json",
            "recommend_cache/user.json", "public_playlists_cache/list.json",
        )
        retained = (
            "lxmusic-service/verify_source.py", "container/lxserver_compat.py",
            "proxy/app.py", "webui-service/static/app.js", ".env.example",
            "webui-service/static/wechat.png", "webui-service/static/alipay.png",
            "container/config.json",
        )
        for rel in (*excluded, *retained):
            path = source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        # Also exercise --delete so stale payload files cannot survive restaging.
        for rel in ("proxy/stale.py",):
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stale")
        text = (FPK_DIR / "build.sh").read_text(encoding="utf-8")
        start = text.index("rsync -a --delete")
        end_marker = '"${REPO_ROOT}/" "${STAGE}/app/repo/"'
        end = text.index(end_marker, start) + len(end_marker)
        subprocess.run(
            ["bash", "-c", text[start:end]], check=True, capture_output=True,
            env={**os.environ, "REPO_ROOT": str(source), "STAGE": str(stage)},
        )
        actual = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
        assert actual == set(retained)
        for name in (".agents", "playlist_tracks", "online_favorites", "cache"):
            assert not list(target.rglob(name))

    def test_runtime_files_present(self, stage: Path):
        repo = stage / "app" / "repo"
        for rel in (
            "install.sh", "extend.sh", "restore.sh",
            "docker-compose.yml", ".env.example", ".dockerignore",
            "fnmusic-ext.service", "VERSION",
            "proxy/app.py", "container/Dockerfile", "webui-service/app.py",
            "lxmusic-service/verify_source.py", "container/lxserver_compat.py",
        ):
            assert (repo / rel).is_file(), f"payload 缺少运行所需文件 {rel}"

    def test_arm64_native_binding_is_embedded(self, stage: Path):
        repo = stage / "app" / "repo"
        version = {}
        for line in (repo / "container/lxserver.version").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key and not key.startswith("#"):
                version[key] = value
        expected = f"music-tag-native-linux-arm64-gnu-{version['LXSERVER_NATIVE_VERSION']}.tgz"
        artifact = repo / "container/lxserver-artifact" / expected
        assert artifact.is_file(), f"FPK 缺少 ARM64 原生模块 {expected}"
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == version["LXSERVER_NATIVE_ARM64_SHA256"]

    def test_no_dev_files_or_secrets(self, stage: Path):
        repo = stage / "app" / "repo"
        for rel in (".env", ".git", ".github", "tests", "docs", "packaging", "dist"):
            assert not (repo / rel).exists(), f"payload 不应包含 {rel}"
        # .env.* 排除但 .env.example 保留
        env_files = [p.name for p in repo.glob(".env*")]
        assert env_files == [".env.example"], f"payload 只应保留 .env.example，实际: {env_files}"

    def test_no_runtime_logs_or_exports(self, stage: Path):
        repo = stage / "app" / "repo"
        assert not list(repo.rglob("logs"))
        assert not list(repo.rglob("*.log"))
        assert not list(repo.rglob("*.logzip"))
        assert not list(repo.rglob("env_snapshot.txt"))

    def test_no_pycache_or_venv(self, stage: Path):
        repo = stage / "app" / "repo"
        assert not list(repo.rglob("__pycache__"))
        assert not [p for p in repo.iterdir() if p.name.startswith(".venv")]
