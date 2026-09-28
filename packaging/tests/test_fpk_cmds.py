"""fpk 应用中心生命周期钩子行为测试（执行真脚本 + PATH 桩）。

test_fpk_pack.py 只做静态结构断言；本文件把 packaging/fpk/cmd/ 的真实脚本
复制进沙箱目录执行，全部外部命令（install.sh/restore.sh/extend.sh/
systemctl/docker/tar/readlink）用 PATH 桩替代，验证各钩子的可观测行为：

- _common：repo 目录三候选定位与优先级、.env 布尔→--sources 推导、数据项通配
- install_init：Docker/官方 socket 两项硬预检
- install_callback：向导答案→install.sh 参数映射（默认回退/必填校验/失败透传）
- upgrade_init：升级前数据 tar 备份（成功内容/失败中止清理）+ 失效 unit 自愈清理
- upgrade_callback：备份恢复→按 .env 推导音源→重跑安装→删备份
- uninstall_init：先还原官方直连（失败降级强制清理，永不阻断卸载），再归档数据
- uninstall_callback：兜底清理永不失败
- main：应用中心 start/stop/status 的退出码契约

不触碰真实 /var/run、/etc、/vol* 与宿主 docker/systemd。
"""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CMD_SRC = REPO_ROOT / "packaging" / "fpk" / "cmd"
BASH = shutil.which("bash")

# 沙箱 PATH 需要的真实工具白名单（cmd 脚本内部使用；docker/systemctl/readlink
# 按测试意图用桩覆盖，tar 在需要验证备份内容时用真 tar、需要故障注入时用桩覆盖）
REAL_TOOLS = ("dirname", "mkdir", "cp", "mv", "rm", "tar", "gzip", "date", "grep",
              "sed", "ls", "tail", "cat", "chmod", "head", "tr", "cut", "tee",
              "readlink")

INSTALLER_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "${STUB_INSTALL_LOG}"
exit "${STUB_INSTALL_RC:-0}"
"""

EXTEND_STUB = """#!/bin/bash
printf 'extend\\n' >> "${STUB_INSTALL_LOG}"
exit "${STUB_EXTEND_RC:-0}"
"""

RESTORE_STUB = """#!/bin/bash
printf 'restore %s\\n' "$*" >> "${STUB_INSTALL_LOG}"
exit "${STUB_RESTORE_RC:-0}"
"""


def _write_stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    # REAL_TOOLS 在此建立过符号链接，直接 write_text 会穿透写到宿主真实二进制
    if path.is_symlink() or path.exists():
        path.unlink()
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class Sandbox:
    """沙箱应用布局：APP_ROOT/cmd（真脚本副本）+ TRIM_APPDEST/repo（假仓库）。"""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "approot"          # = cmd/.. 即 FNMUSIC_APP_ROOT
        self.cmd = self.root / "cmd"
        self.cmd.mkdir(parents=True)
        for entry in CMD_SRC.iterdir():
            shutil.copy2(entry, self.cmd / entry.name)
        self.appdest = tmp_path / "appdest"       # TRIM_APPDEST
        self.appdest.mkdir()
        self.pkgvar = tmp_path / "pkgvar"         # TRIM_PKGVAR
        self.pkgvar.mkdir()
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        for tool in (*REAL_TOOLS, "bash"):
            real = shutil.which(tool)
            if real:
                (self.bindir / tool).symlink_to(real)
        self.install_log = tmp_path / "logs" / "install.log"
        self.install_log.parent.mkdir()
        self.durable_log = self.install_log.parent / "durable-install.log"
        self.ctl_log = tmp_path / "logs" / "systemctl.log"
        self.docker_log = tmp_path / "logs" / "docker.log"
        self.tar_log = tmp_path / "logs" / "tar.log"
        self.readlink_out = ""
        self.is_active_rc = 3

    # ---- 桩命令 ---------------------------------------------------------
    def add_docker(self, rc: int = 0) -> None:
        _write_stub(self.bindir, "docker",
                    f'#!/bin/bash\nprintf \'docker %s\\n\' "$*" >> "{self.docker_log}"\nexit {rc}\n')

    def add_systemctl(self) -> None:
        _write_stub(self.bindir, "systemctl", f"""#!/bin/bash
printf 'systemctl %s\\n' "$*" >> "{self.ctl_log}"
case "$1" in
  is-active) exit {self.is_active_rc} ;;
  *) exit 0 ;;
esac
""")

    def add_tar(self, rc: int = 0) -> None:
        # 只记录参数不落盘（避免向真实 /vol* 写归档）
        _write_stub(self.bindir, "tar",
                    f'#!/bin/bash\nprintf \'tar %s\\n\' "$*" >> "{self.tar_log}"\nexit {rc}\n')

    def add_readlink(self, out: str) -> None:
        _write_stub(self.bindir, "readlink",
                    f'#!/bin/bash\nprintf \'%s\\n\' "{out}"\n')

    # ---- 布局辅助 -------------------------------------------------------
    def make_repo(self, env_text: str | None = None, stub: str = INSTALLER_STUB,
                  with_restore: bool = False, with_compose: bool = False) -> Path:
        repo = self.appdest / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "install.sh").write_text(stub, encoding="utf-8")
        (repo / "install.sh").chmod(0o755)
        if env_text is not None:
            (repo / ".env").write_text(env_text, encoding="utf-8")
        if with_restore:
            (repo / "restore.sh").write_text(RESTORE_STUB, encoding="utf-8")
            (repo / "restore.sh").chmod(0o755)
        if with_compose:
            (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        return repo

    def add_data(self, repo: Path) -> None:
        (repo / "sources-data").mkdir(exist_ok=True)
        (repo / "sources-data" / "keep.txt").write_text("data", encoding="utf-8")
        (repo / "play_history").mkdir(exist_ok=True)
        (repo / "play_history" / "u.json").write_text("[]", encoding="utf-8")

    # ---- 执行 -----------------------------------------------------------
    def env(self, **extra: str) -> dict:
        env = {
            "PATH": str(self.bindir),
            "TRIM_APPDEST": str(self.appdest),
            "TRIM_PKGVAR": str(self.pkgvar),
            "TRIM_TEMP_LOGFILE": str(self.tmp / "trim-log.txt"),
            "STUB_INSTALL_LOG": str(self.install_log),
            "FNMUSIC_DURABLE_LOG": str(self.durable_log),
            "HOME": str(self.tmp),
        }
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def run(self, script: str, *args: str, env: dict | None = None,
            **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.cmd / script), *args],
                              env=env or self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def bash(self, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", body], env=env or self.env(),
                              capture_output=True, text=True, timeout=60)

    def install_args(self) -> list[str]:
        if not self.install_log.exists():
            return []
        return self.install_log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sb(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def read(sb: Sandbox, name: str) -> str:
    return (sb.cmd / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- _common ---

def test_common_repo_dir_precedence(sb):
    """TRIM_APPDEST/repo 优先，其次 APP_ROOT/target/repo，最后 APP_ROOT/repo。"""
    sb.make_repo(env_text="A=1")
    target = sb.root / "target" / "repo"
    target.mkdir(parents=True)
    (target / "install.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    local = sb.root / "repo"
    local.mkdir()
    (local / "install.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")

    script = f'set -uo pipefail\nSELF_DIR="{sb.cmd}"\n. "{sb.cmd}/_common"\nprintf \'%s\\n\' "$FNMUSIC_REPO_DIR"\n'
    # 1) TRIM_APPDEST 命中优先
    out = sb.bash(script)
    assert out.stdout.strip() == str(sb.appdest / "repo")
    # 2) TRIM_APPDEST 空目录时回退 target/repo
    empty = sb.tmp / "emptydest"
    empty.mkdir()
    out = sb.bash(script, env=sb.env(TRIM_APPDEST=str(empty)))
    assert out.stdout.strip() == str(target)
    # 3) 全无候选（缺 install.sh 的目录不算 repo）→ FNMUSIC_REPO_DIR 为空
    (target / "install.sh").unlink()
    (local / "install.sh").unlink()
    out = sb.bash(script, env=sb.env(TRIM_APPDEST=str(empty)))
    assert out.stdout.strip() == ""


def test_common_saved_source_priority(sb):
    """.env 三布尔→--sources：musicbox > lxmusic > musicdl，未配置为空。"""
    cases = [
        ("FNMUSIC_NETEASE_ENABLED=true\n", "musicbox"),
        ("FNMUSIC_LX_ENABLED=true\n", "lxmusic"),
        ("FNMUSIC_MUSICDL_ENABLED=true\n", "musicdl"),
        # 三者并存取最高优先级，避免升级后源漂移
        ("FNMUSIC_NETEASE_ENABLED=true\nFNMUSIC_LX_ENABLED=true\nFNMUSIC_MUSICDL_ENABLED=true\n", "musicbox"),
        ("FNMUSIC_LX_ENABLED=true\nFNMUSIC_MUSICDL_ENABLED=true\n", "lxmusic"),
        ("FNMUSIC_MUSICDL_ENABLED=false\n", ""),
        ("", ""),
    ]
    script = (f'set -uo pipefail\nSELF_DIR="{sb.cmd}"\n. "{sb.cmd}/_common"\n'
              'printf \'%s\' "$(fnmusic_saved_source)"\n')
    for env_text, expected in cases:
        sb.make_repo(env_text=env_text or None)
        out = sb.bash(script)
        assert out.returncode == 0, out.stderr
        assert out.stdout == expected, (env_text, out.stdout)
        # 用完即弃，下一轮重建干净 repo
        shutil.rmtree(sb.appdest / "repo")


def test_common_saved_lx_url(sb):
    sb.make_repo(env_text="LX_SOURCE_URL=http://s/x.js\nOTHER=1\n")
    script = (f'set -uo pipefail\nSELF_DIR="{sb.cmd}"\n. "{sb.cmd}/_common"\n'
              'printf \'%s\' "$(fnmusic_saved_lx_url)"\n')
    out = sb.bash(script)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "http://s/x.js"
    # 重复键取最后一行（env_merge 增量合并的既有形态）
    sb.make_repo(env_text="LX_SOURCE_URL=http://old/x.js\nLX_SOURCE_URL=http://new/y.js\n")
    out = sb.bash(script)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "http://new/y.js"


def test_common_data_items_only_existing(sb, tmp_path):
    repo = sb.make_repo(env_text="X=1\n")
    (repo / ".env.bak.20260101000000").write_text("X=0\n", encoding="utf-8")
    sb.add_data(repo)
    (repo / "online_favorites.json").write_text("{}", encoding="utf-8")
    # 诱饵 cwd：含有 .env.bak* 时 for 列表不得在 cwd 提前展开（曾致备份漏文件）
    decoy_cwd = tmp_path / "decoy-cwd"
    decoy_cwd.mkdir()
    (decoy_cwd / ".env.bak.FROM_CWD").write_text("", encoding="utf-8")
    script = (f'set -uo pipefail\nSELF_DIR="{sb.cmd}"\n. "{sb.cmd}/_common"\n'
              'printf \'%s\' "$(fnmusic_data_items)"\n')
    out = subprocess.run([BASH, "-c", script], env=sb.env(), cwd=decoy_cwd,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    items = set(out.stdout.split())
    # 返回 repo 内真实文件名（已展开），不得带入调用方 cwd 的诱饵
    assert items == {".env", ".env.bak.20260101000000", "sources-data",
                     "play_history", "online_favorites.json"}
    assert "FROM_CWD" not in out.stdout
    assert ".env.bak*" not in items
    # 全空仓库 → 空串（后续 tar/归档直接跳过）
    shutil.rmtree(repo)
    sb.make_repo(env_text=None)
    out = sb.bash(script)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""


# ------------------------------------------------------------ install_init ---

def _socket_variant(sb: Sandbox, sock: Path) -> None:
    """把 install_init 里硬编码的官方 socket 路径重定向到沙箱（逻辑不变）。"""
    text = read(sb, "install_init").replace("/var/run/trim_music.socket", str(sock))
    (sb.cmd / "install_init").write_text(text, encoding="utf-8")
    (sb.cmd / "install_init").chmod(0o755)


@pytest.fixture
def held_socket(tmp_path):
    """保持 inode 存活的临时 Unix socket（bind 后不关闭）。"""
    path = tmp_path / "trim_music.socket"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    try:
        yield path
    finally:
        s.close()


def test_install_init_fails_without_docker(sb, tmp_path):
    _socket_variant(sb, tmp_path / "absent.sock")
    result = sb.run("install_init")
    assert result.returncode == 1
    assert "Docker" in result.stderr
    # 失败原因必须写入 TRIM_TEMP_LOGFILE（应用中心进度 UI 展示）
    assert "Docker" in (tmp_path / "trim-log.txt").read_text(encoding="utf-8")


def test_install_init_fails_without_official_socket(sb, tmp_path):
    sb.add_docker()
    _socket_variant(sb, tmp_path / "absent.sock")
    result = sb.run("install_init")
    assert result.returncode == 1
    assert "飞牛音乐" in result.stderr


def test_install_init_passes_with_both_prerequisites(sb, tmp_path, held_socket):
    sb.add_docker()
    _socket_variant(sb, held_socket)
    result = sb.run("install_init")
    assert result.returncode == 0, result.stderr


# -------------------------------------------------------- install_callback ---

def test_install_callback_defaults_invalid_source_to_musicdl(sb):
    sb.make_repo()
    result = sb.run("install_callback", wizard_sources="bogus", wizard_extend="true")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == ["--non-interactive --sources musicdl --webui --extend"]


def test_install_callback_lx_without_url_installs_sourceless(sb):
    """向导不再索要洛雪源 URL：无值=无源安装（装后在管理页配置），不得失败。"""
    sb.make_repo()
    result = sb.run("install_callback", wizard_sources="lxmusic", wizard_lx_url="")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == ["--non-interactive --sources lxmusic --webui --extend"]


def test_install_callback_lx_url_passthrough_and_extend_off(sb):
    sb.make_repo()
    result = sb.run("install_callback", wizard_sources="lxmusic",
                    wizard_lx_url="http://s/y.js", wizard_extend="false")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == [
        "--non-interactive --sources lxmusic --webui --lx-source-url http://s/y.js"]


def test_install_callback_legacy_lx_url_still_passthrough(sb):
    """旧版向导写入的向导值仍透传（升级兼容）；skip-verify 开关随向导字段一并移除。"""
    sb.make_repo()
    result = sb.run("install_callback", wizard_sources="lxmusic",
                    wizard_lx_url="http://s/y.js", wizard_lx_skip_verify="true")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == [
        "--non-interactive --sources lxmusic --webui --lx-source-url http://s/y.js --extend"]


def test_install_callback_lx_skip_verify_off_by_default(sb):
    sb.make_repo()
    result = sb.run("install_callback", wizard_sources="lxmusic",
                    wizard_lx_url="http://s/y.js", wizard_lx_skip_verify="false")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == [
        "--non-interactive --sources lxmusic --webui --lx-source-url http://s/y.js --extend"]


def test_install_callback_install_failure_tails_log(sb):
    sb.make_repo()
    log = sb.pkgvar / "fnmusic-app.log"
    log.write_text("\n".join(f"line{i}" for i in range(1, 8)) + "\n", encoding="utf-8")
    result = sb.run("install_callback", wizard_sources="musicdl",
                    STUB_INSTALL_RC="7")
    assert result.returncode == 1
    assert "安装失败" in result.stderr
    # stderr 里带出日志尾部 5 行，应用中心 UI 可见失败原因
    for i in range(3, 8):
        assert f"line{i}" in result.stderr
    assert "line2" not in result.stderr


def test_install_callback_install_failure_surfaces_error_lines(sb):
    """安装失败：日志里的 [ERROR] 行必须进入弹窗文案（回滚会删除日志文件）。"""
    sb.make_repo()
    log = sb.pkgvar / "fnmusic-app.log"
    log.write_text(
        "[INFO] ok step\n"
        "\033[31m[ERROR]\033[0m 洛雪源校验未通过（resolve）：源脚本解析失败\n"
        "\033[31m[ERROR]\033[0m 可更换源 URL 或勾选跳过校验\n",
        encoding="utf-8")
    result = sb.run("install_callback", wizard_sources="musicdl",
                    STUB_INSTALL_RC="7")
    assert result.returncode == 1
    # 错误行进入 stderr（fnmusic_fail），tail 原始日志允许带色码
    assert "洛雪源校验未通过（resolve）" in result.stderr
    # 失败原因写入 TRIM_TEMP_LOGFILE（应用中心弹窗文案），必须剥离 ANSI 色码
    trim_log = (sb.tmp / "trim-log.txt").read_text(encoding="utf-8")
    assert "洛雪源校验未通过（resolve）" in trim_log
    assert "\033[31m" not in trim_log


def test_install_callback_ignores_buildkit_run_title_with_error_echo(sb):
    """BuildKit 步骤标题含 echo "[ERROR]" 时弹窗必须显示真实 [ERROR]，不能是 fix_dns。"""
    sb.make_repo()
    log = sb.pkgvar / "fnmusic-app.log"
    log.write_text(
        "#18 [ 3/20] RUN set -eu;  fix_dns() { probe_host=\"deb.debian.org\"; "
        "echo \"[ERROR] apt 镜像源与官方源均未取到索引\"; }\n"
        "\033[31m[ERROR]\033[0m 等待 musicdl healthz 超时\n",
        encoding="utf-8")
    result = sb.run("install_callback", wizard_sources="musicdl",
                    STUB_INSTALL_RC="7")
    assert result.returncode == 1
    trim_log = (sb.tmp / "trim-log.txt").read_text(encoding="utf-8")
    assert "等待 musicdl healthz 超时" in trim_log
    assert "fix_dns" not in trim_log
    assert "apt 镜像源" not in trim_log


def test_install_callback_copies_output_to_durable_log(sb):
    """安装过程写到回滚删不掉的日志，弹窗给出该路径而不是 TRIM_PKGVAR。"""
    sb.make_repo(stub="""#!/bin/bash
echo "durable-marker"
echo -e "\\033[31m[ERROR]\\033[0m 容器健康检查失败"
exit 7
""")
    result = sb.run("install_callback", wizard_sources="musicdl", wizard_extend="false")
    assert result.returncode == 1
    durable = sb.durable_log.read_text(encoding="utf-8")
    assert "durable-marker" in durable
    assert "容器健康检查失败" in durable
    trim_log = (sb.tmp / "trim-log.txt").read_text(encoding="utf-8")
    assert str(sb.durable_log) in trim_log
    assert "容器健康检查失败" in trim_log
    assert str(sb.pkgvar / "fnmusic-app.log") not in trim_log


def test_install_callback_requires_repo_payload(sb):
    result = sb.run("install_callback", wizard_sources="musicdl")
    assert result.returncode == 1
    assert "应用文件缺失" in result.stderr


# ------------------------------------------------------------ upgrade_init ---

def test_upgrade_init_backup_ignores_caller_cwd_env_bak(sb, tmp_path):
    """调用方 cwd 有 .env.bak* 时，tar 不得按 cwd 展开导致备份失败。"""
    repo = sb.make_repo(env_text="FNMUSIC_MUSICDL_ENABLED=true\n")
    (repo / ".env.bak.20260101000000").write_text("X=0\n", encoding="utf-8")
    sb.add_data(repo)
    sb.add_systemctl()
    decoy = tmp_path / "decoy-cwd"
    decoy.mkdir()
    (decoy / ".env.bak.FROM_CWD").write_text("", encoding="utf-8")
    # FNMUSIC_UNIT_FILE 指向不存在的沙箱路径：开发机上 /etc 真有本服务 unit，
    # 不注入会让自愈逻辑删掉真实文件，且用例行为随宿主机漂移
    result = subprocess.run([str(sb.cmd / "upgrade_init")],
                            env=sb.env(FNMUSIC_UNIT_FILE=str(sb.tmp / "absent-unit.service")),
                            cwd=decoy,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    backup = sb.pkgvar / "upgrade-backup" / "data.tar.gz"
    assert backup.is_file()
    listing = subprocess.run(["tar", "-tzf", str(backup)],
                             capture_output=True, text=True, check=True).stdout
    assert ".env.bak.20260101000000" in listing.splitlines()
    assert "FROM_CWD" not in listing


def test_upgrade_init_backs_up_data_and_stops_service(sb):
    repo = sb.make_repo(env_text="FNMUSIC_LX_ENABLED=true\n")
    sb.add_data(repo)
    sb.add_systemctl()
    result = sb.run("upgrade_init", FNMUSIC_UNIT_FILE=str(sb.tmp / "absent-unit.service"))
    assert result.returncode == 0, result.stderr
    # 先停服务
    assert "stop fnmusic-ext.service" in sb.ctl_log.read_text(encoding="utf-8")
    # 备份内容覆盖 .env 与数据目录（真 tar 校验）
    backup = sb.pkgvar / "upgrade-backup" / "data.tar.gz"
    assert backup.is_file()
    listing = subprocess.run(["tar", "-tzf", str(backup)],
                             capture_output=True, text=True, check=True).stdout
    assert ".env" in listing.splitlines()
    assert any(entry.startswith("sources-data/") for entry in listing.splitlines())
    assert any(entry.startswith("play_history/") for entry in listing.splitlines())


def test_upgrade_init_tar_failure_aborts_and_cleans(sb):
    repo = sb.make_repo(env_text="X=1\n")
    sb.add_data(repo)
    sb.add_tar(rc=1)
    before = (repo / ".env").read_text(encoding="utf-8")
    result = sb.run("upgrade_init", FNMUSIC_UNIT_FILE=str(sb.tmp / "absent-unit.service"))
    assert result.returncode == 1
    assert "备份数据失败" in result.stderr
    # 失败后备份目录清理、原数据不动
    assert not (sb.pkgvar / "upgrade-backup").exists()
    assert (repo / ".env").read_text(encoding="utf-8") == before


def test_upgrade_init_no_repo_still_stops_service(sb):
    sb.add_systemctl()
    result = sb.run("upgrade_init")
    assert result.returncode == 0, result.stderr
    assert "stop fnmusic-ext.service" in sb.ctl_log.read_text(encoding="utf-8")


def test_upgrade_init_empty_repo_makes_no_backup(sb):
    sb.make_repo(env_text=None)
    sb.add_systemctl()
    result = sb.run("upgrade_init", FNMUSIC_UNIT_FILE=str(sb.tmp / "absent-unit.service"))
    assert result.returncode == 0, result.stderr
    assert not (sb.pkgvar / "upgrade-backup" / "data.tar.gz").exists()


def test_upgrade_init_removes_stale_unit_pointing_elsewhere(sb):
    """迁卷自愈：unit 指向其他目录时删掉失效 unit 并 daemon-reload，升级不再卡死。"""
    repo = sb.make_repo(env_text="FNMUSIC_LX_ENABLED=true\n")
    sb.add_data(repo)
    sb.add_systemctl()
    stale = sb.tmp / "stale-unit.service"
    stale.write_text("[Service]\nWorkingDirectory=/vol1/old/location\n", encoding="utf-8")
    result = sb.run("upgrade_init", FNMUSIC_UNIT_FILE=str(stale))
    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    ctl = sb.ctl_log.read_text(encoding="utf-8")
    assert "stop fnmusic-ext.service" in ctl
    assert "daemon-reload" in ctl
    # 自愈不阻断备份
    assert (sb.pkgvar / "upgrade-backup" / "data.tar.gz").is_file()


def test_upgrade_init_keeps_unit_matching_this_repo(sb):
    """unit 属于本次安装目录：不删、不 daemon-reload，交由归属检查正常放行。"""
    repo = sb.make_repo(env_text="FNMUSIC_LX_ENABLED=true\n")
    sb.add_data(repo)
    sb.add_systemctl()
    unit = sb.tmp / "live-unit.service"
    unit.write_text(f"[Service]\nWorkingDirectory={repo}\n", encoding="utf-8")
    result = sb.run("upgrade_init", FNMUSIC_UNIT_FILE=str(unit))
    assert result.returncode == 0, result.stderr
    assert unit.exists()
    assert "daemon-reload" not in sb.ctl_log.read_text(encoding="utf-8")


# -------------------------------------------------------- upgrade_callback ---

def _make_backup(sb: Sandbox, repo: Path, env_text: str) -> None:
    (repo / ".env").write_text(env_text, encoding="utf-8")
    backup = sb.pkgvar / "upgrade-backup"
    backup.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-czf", str(backup / "data.tar.gz"),
                    "-C", str(repo), ".env"], check=True)


def test_upgrade_callback_restores_backup_and_reinstalls_lx(sb):
    repo = sb.make_repo(env_text="FNMUSIC_MUSICDL_ENABLED=true\n")  # 旧值，应被备份覆盖
    _make_backup(sb, repo, "FNMUSIC_LX_ENABLED=true\nLX_SOURCE_URL=http://s/x.js\n")
    sb.add_data(repo)
    result = sb.run("upgrade_callback")
    assert result.returncode == 0, result.stderr
    # 备份里的 .env 已恢复（升级后音源与 URL 都来自备份）；
    # 升级跳过洛雪源可用性校验（--lx-skip-verify）：源服务器临时故障不得卡死升级
    assert "FNMUSIC_LX_ENABLED=true" in (repo / ".env").read_text(encoding="utf-8")
    assert sb.install_args() == [
        "--non-interactive --sources lxmusic --webui --extend --lx-source-url http://s/x.js --lx-skip-verify"]
    # 成功后备份目录删除
    assert not (sb.pkgvar / "upgrade-backup").exists()


def test_upgrade_callback_corrupt_backup_fails_and_keeps_it(sb):
    repo = sb.make_repo(env_text="FNMUSIC_MUSICDL_ENABLED=true\n")
    backup = sb.pkgvar / "upgrade-backup"
    backup.mkdir()
    (backup / "data.tar.gz").write_bytes(b"not-a-tar")
    result = sb.run("upgrade_callback")
    assert result.returncode == 1
    assert "恢复用户数据失败" in result.stderr
    # 备份保留（用户逃生通道），install.sh 未被调用
    assert (backup / "data.tar.gz").is_file()
    assert sb.install_args() == []


def test_upgrade_callback_lx_without_url_still_upgrades(sb):
    """v2.2.7+ 无源安装（装后管理页配置）：.env 缺 LX_SOURCE_URL 不阻塞升级。"""
    repo = sb.make_repo()
    _make_backup(sb, repo, "FNMUSIC_LX_ENABLED=true\n")  # 备份缺 LX_SOURCE_URL
    result = sb.run("upgrade_callback")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == ["--non-interactive --sources lxmusic --webui --extend --lx-skip-verify"]
    # 升级成功后备份清理
    assert not (sb.pkgvar / "upgrade-backup" / "data.tar.gz").exists()


def test_upgrade_callback_without_backup_uses_current_env(sb):
    sb.make_repo(env_text="FNMUSIC_NETEASE_ENABLED=true\n")
    result = sb.run("upgrade_callback")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == ["--non-interactive --sources musicbox --webui --extend"]


def test_upgrade_callback_install_failure_keeps_backup(sb):
    repo = sb.make_repo(env_text="FNMUSIC_MUSICDL_ENABLED=true\n")
    sb.add_data(repo)
    _make_backup(sb, repo, "FNMUSIC_MUSICDL_ENABLED=true\n")
    result = sb.run("upgrade_callback", STUB_INSTALL_RC="9")
    assert result.returncode == 1
    assert "升级失败" in result.stderr
    assert (sb.pkgvar / "upgrade-backup" / "data.tar.gz").is_file()


def test_upgrade_callback_defaults_to_musicdl_when_env_silent(sb):
    sb.make_repo(env_text="SOME_OTHER_KEY=1\n")
    result = sb.run("upgrade_callback")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == ["--non-interactive --sources musicdl --webui --extend"]


# ---------------------------------------------------------- uninstall_init ---

def test_uninstall_init_degrades_when_restore_fails(sb):
    """还原失败不再中止卸载：常规→--adopt 均失败后强制清理，数据归档照常完成。"""
    repo = sb.make_repo(with_restore=True)
    sb.add_data(repo)
    sb.add_tar()
    sb.add_systemctl()
    sb.add_docker()
    unit = sb.tmp / "sandbox-unit.service"
    unit.write_text(f"[Service]\nWorkingDirectory={repo}\n", encoding="utf-8")
    result = sb.run("uninstall_init", STUB_RESTORE_RC="3", FNMUSIC_UNIT_FILE=str(unit))
    assert result.returncode == 0, result.stderr
    # 两种还原方式都尝试过：常规 + --adopt
    calls = sb.install_log.read_text(encoding="utf-8").splitlines()
    assert any(line.strip() == "restore" for line in calls)
    assert "restore --adopt" in calls
    # 降级清理：停用并删除 unit 文件、删音源容器
    ctl = sb.ctl_log.read_text(encoding="utf-8")
    assert "stop fnmusic-ext.service" in ctl
    assert "daemon-reload" in ctl
    assert not unit.exists()
    assert "rm -f fnmusic-sources" in sb.docker_log.read_text(encoding="utf-8")
    # 数据归档没有被还原失败阻断
    assert "czf" in sb.tar_log.read_text(encoding="utf-8")


def test_uninstall_init_keep_data_false_skips_archive(sb):
    repo = sb.make_repo(with_restore=True)
    sb.add_data(repo)
    sb.add_tar()
    result = sb.run("uninstall_init", wizard_keep_data="false")
    assert result.returncode == 0, result.stderr
    assert not sb.tar_log.exists()


def test_uninstall_init_archives_to_volume_root(sb):
    repo = sb.make_repo(env_text="X=1\n", with_restore=True)
    sb.add_data(repo)
    sb.add_tar()
    # target symlink 解析到 /vol5/@appcenter/fnmusic-ext（桩 readlink）
    sb.add_readlink("/vol5/@appcenter/fnmusic-ext")
    result = sb.run("uninstall_init")
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    archive = lines[0]
    assert archive.startswith("/vol5/fnmusic-ext-backup-")
    assert archive.endswith(".tar.gz")
    # tar 参数：归档路径 + 以 repo 为基点的数据项
    tar_args = sb.tar_log.read_text(encoding="utf-8")
    assert f"tar -czf {archive} -C {repo}" in tar_args
    for item in (".env", "sources-data", "play_history"):
        assert item in tar_args


def test_uninstall_init_without_repo_exits_cleanly(sb):
    sb.add_tar()
    result = sb.run("uninstall_init")
    assert result.returncode == 0, result.stderr
    assert not sb.tar_log.exists()


# ------------------------------------------------------ uninstall_callback ---

def test_uninstall_callback_never_fails_even_if_all_cleanup_fails(sb):
    sb.make_repo(with_compose=True)
    rm_log = sb.tmp / "logs" / "rm.log"
    # systemctl/docker 全部失败、rm 仅记录：既验证"永不失败"，又绝不触达宿主 /etc
    _write_stub(sb.bindir, "systemctl", "#!/bin/bash\nexit 1\n")
    _write_stub(sb.bindir, "docker", "#!/bin/bash\nexit 1\n")
    _write_stub(sb.bindir, "rm", f'#!/bin/bash\nprintf \'%s\\n\' "$*" >> "{rm_log}"\nexit 0\n')
    result = sb.run("uninstall_callback")
    assert result.returncode == 0, result.stderr
    assert "/etc/systemd/system/fnmusic-ext.service" in rm_log.read_text(encoding="utf-8")


# -------------------------------------------------------------------- main ---

def test_main_start_requires_env(sb):
    repo = sb.make_repo(env_text=None)
    result = sb.run("main", "start")
    assert result.returncode == 1
    assert "尚未完成安装配置" in result.stderr


def test_main_start_runs_idempotent_extend(sb):
    repo = sb.make_repo(env_text="X=1\n", stub=EXTEND_STUB)
    (repo / "extend.sh").write_text(EXTEND_STUB, encoding="utf-8")
    (repo / "extend.sh").chmod(0o755)
    result = sb.run("main", "start")
    assert result.returncode == 0, result.stderr
    assert sb.install_args() == ["extend"]


def test_main_start_failure_surfaces_log_tail(sb):
    sb.make_repo(env_text="X=1\n", stub=EXTEND_STUB)
    log = sb.pkgvar / "fnmusic-app.log"
    log.write_text("boom-detail\n", encoding="utf-8")
    result = sb.run("main", "start", STUB_EXTEND_RC="4")
    assert result.returncode == 1
    assert "启动失败" in result.stderr
    assert "boom-detail" in result.stderr


def test_main_stop_is_idempotent_even_when_all_fail(sb):
    sb.make_repo(with_compose=True)
    _write_stub(sb.bindir, "systemctl", "#!/bin/bash\nexit 1\n")
    _write_stub(sb.bindir, "docker", "#!/bin/bash\nexit 1\n")
    result = sb.run("main", "stop")
    assert result.returncode == 0, result.stderr


def test_main_status_exit_codes(sb):
    sb.make_repo()
    sb.add_systemctl()
    sb.is_active_rc = 0
    sb.add_systemctl()
    assert sb.run("main", "status").returncode == 0
    sb.is_active_rc = 3
    sb.add_systemctl()
    assert sb.run("main", "status").returncode == 3
    assert sb.run("main", "bogus").returncode == 1
