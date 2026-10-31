"""容器组装离线测试：supervisord.conf / entrypoint 开关选择 / compose 单服务 / Dockerfile。

不依赖 docker：entrypoint 用假的 supervisord/supervisorctl 走真 shell 逻辑。
"""
import ast
import configparser
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

CONTAINER_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONTAINER_DIR.parent


@pytest.fixture(scope="module")
def supervisord_conf():
    cp = configparser.ConfigParser()
    cp.read(CONTAINER_DIR / "supervisord.conf")
    return cp


def test_supervisord_programs_all_autostart_false(supervisord_conf):
    programs = sorted(s for s in supervisord_conf.sections() if s.startswith("program:"))
    assert programs == [
        "program:lxmusic", "program:lxserver", "program:musicbox", "program:musicdl", "program:webui",
    ]
    for section in programs:
        assert supervisord_conf.get(section, "autostart") == "false"
        assert supervisord_conf.get(section, "autorestart") == "true"
        # 组内进程一起停，避免 uvicorn 子进程残留
        assert supervisord_conf.get(section, "stopasgroup") == "true"
        assert supervisord_conf.get(section, "killasgroup") == "true"
        assert supervisord_conf.get(section, "redirect_stderr") == "true"
        # 日志默认汇到容器 stdout（docker logs 聚合）。lxserver 是唯一例外：
        # issue #108 已证实 stdout 单向管道读不回来，管理页导出拿不到它的运行
        # 日志，因此改为落盘到共享日志根，详见 test_lxserver_logging.py。
        if section == "program:lxserver":
            assert supervisord_conf.get(section, "stdout_logfile").startswith("/data/logs/")
            continue
        assert supervisord_conf.get(section, "stdout_logfile") == "/dev/stdout"
        assert supervisord_conf.get(section, "stdout_logfile_maxbytes") == "0"


def test_supervisord_ports_and_directories(supervisord_conf):
    expected = {
        "musicdl": ("8001", "/srv/musicdl-service"),
        "musicbox": ("8002", "/srv/musicbox-service"),
        "lxmusic": ("8003", "/srv/lxmusic-service"),
        "webui": ("8004", "/srv/webui-service"),
    }
    for prog, (port, directory) in expected.items():
        section = f"program:{prog}"
        command = supervisord_conf.get(section, "command")
        assert f"--port {port}" in command
        assert supervisord_conf.get(section, "directory") == directory

    # lxserver 独立 node 进程，通过环境变量注入端口与路径
    lxserver_sec = "program:lxserver"
    assert supervisord_conf.get(lxserver_sec, "directory") == "/srv/lxserver"
    assert "node index.js" in supervisord_conf.get(lxserver_sec, "command")
    assert 'PORT="8005"' in supervisord_conf.get(lxserver_sec, "environment")


def test_supervisord_daemon_section(supervisord_conf):
    assert supervisord_conf.get("supervisord", "nodaemon") == "true"
    assert supervisord_conf.get("unix_http_server", "chmod") == "0700"
    assert supervisord_conf.get("supervisorctl", "serverurl") == "unix:///tmp/supervisor.sock"


# ---------------------------------------------------------------- env_flag.sh

def _run_env_flag(env_file: Path, key: str, env_extra: dict | None = None, default: str = "") -> str:
    """source env_flag.sh 后调用 env_flag key，返回标准化输出。"""
    lib = CONTAINER_DIR / "env_flag.sh"
    script = f'. "{lib}"; env_flag {key} {default}\n'
    env = {"PATH": os.environ["PATH"], "FNMUSIC_ENV_FILE": str(env_file)}
    env.update(env_extra or {})
    out = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env=env, timeout=15,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_env_flag_reads_quoted_and_export_lines(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FNMUSIC_LX_ENABLED='true'\n"
        "export FNMUSIC_WEBUI_ENABLED=\"true\"\n"
        "FNMUSIC_MUSICDL_ENABLED=false\n",
        encoding="utf-8",
    )
    assert _run_env_flag(env_file, "FNMUSIC_LX_ENABLED") == "true"
    assert _run_env_flag(env_file, "FNMUSIC_WEBUI_ENABLED") == "true"
    assert _run_env_flag(env_file, "FNMUSIC_MUSICDL_ENABLED") == "false"


def test_env_flag_env_var_and_default_fallback(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER_KEY=1\n", encoding="utf-8")
    # .env 无该键 → 进程环境变量
    assert _run_env_flag(env_file, "FNMUSIC_LX_ENABLED", {"FNMUSIC_LX_ENABLED": "yes"}) == "true"
    # 都没有 → 默认值
    assert _run_env_flag(env_file, "FNMUSIC_NETEASE_ENABLED") == "false"
    assert _run_env_flag(env_file, "FNMUSIC_NETEASE_ENABLED", default="true") == "true"
    # 非真值关键词一律 false
    assert _run_env_flag(env_file, "FNMUSIC_X", {"FNMUSIC_X": "随便"}) == "false"


def test_env_flag_last_definition_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FNMUSIC_LX_ENABLED=false\nFNMUSIC_LX_ENABLED=true\n", encoding="utf-8")
    assert _run_env_flag(env_file, "FNMUSIC_LX_ENABLED") == "true"


def test_export_source_env_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LX_SOURCE_URL='https://example.com/lx.js'\n"
        "LX_SOURCE_LIST='[{\"name\":\"a\",\"url\":\"https://example.com/lx.js\",\"active\":true}]'\n"
        "LX_SOURCES=kw,kg\n"
        "MUSICDL_SOURCES=kugou,netease\n",
        encoding="utf-8",
    )
    lib = CONTAINER_DIR / "env_flag.sh"
    script = (f'. "{lib}"; export_source_env; printenv LX_SOURCE_URL; printenv LX_SOURCE_LIST; '
              'printenv LX_SOURCES; printenv MUSICDL_SOURCES\n')
    out = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "FNMUSIC_ENV_FILE": str(env_file)}, timeout=15,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines == ["https://example.com/lx.js",
                     "[{\"name\":\"a\",\"url\":\"https://example.com/lx.js\",\"active\":true}]",
                     "kw,kg", "kugou,netease"]


# ---------------------------------------------------------------- entrypoint.sh

class FakeSupervisor:
    """假 supervisord/supervisorctl：记录调用，status 恒可用。"""

    def __init__(self, bindir: Path, log: Path):
        ctl = bindir / "supervisorctl"
        ctl.write_text(
            textwrap.dedent(f"""\
            #!/bin/sh
            echo "$@" >> "{log}"
            case "$1" in
              pid) exit 0 ;;
              *) exit 0 ;;
            esac
            """),
            encoding="utf-8",
        )
        ctl.chmod(0o755)
        daemon = bindir / "supervisord"
        daemon.write_text(
            textwrap.dedent(f"""\
            #!/bin/sh
            echo "supervisord $*" >> "{log}"
            sleep 0.3
            exit 0
            """),
            encoding="utf-8",
        )
        daemon.chmod(0o755)


def _run_entrypoint(fake_bin: Path, env_file: Path, log: Path, tmp_path: Path | None = None) -> list[str]:
    """跑真实 entrypoint.sh，返回 supervisorctl 的调用序列（去掉 supervisord 行）。"""
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ENV_FLAG_LIB": str(CONTAINER_DIR / "env_flag.sh"),
        "SUPERVISOR_CONF": "/nonexistent/supervisord.conf",
        "FNMUSIC_ENV_FILE": str(env_file),
    }
    if tmp_path is not None:
        data_dir = tmp_path / "data"
        env["DATA_PATH"] = str(data_dir / "lxserver")
        env["LX_DATA_DIR"] = str(data_dir / "lxmusic")
        # 镜像容器形态的 XDG_DATA_HOME=/data：musicbox cookie 启发判断用
        env["XDG_DATA_HOME"] = str(data_dir)
        # 真实环境 Dockerfile ENV 与 native systemd unit 都导出日志根；
        # 宿主测试不设会落到只读 /repo，entrypoint 建目录即失败
        env["FNMUSIC_LOG_DIR"] = str(tmp_path / "logs")
    proc = subprocess.run(
        ["sh", str(CONTAINER_DIR / "entrypoint.sh")],
        capture_output=True, text=True, timeout=30,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    calls = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("supervisord ")]
    calls = [c.replace("-c /nonexistent/supervisord.conf ", "") for c in calls]
    # 套接字就绪探测的 pid 轮询不算启动动作
    return [c for c in calls if c != "pid"]


@pytest.fixture()
def entrypoint_env(tmp_path):
    log = tmp_path / "ctl.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    FakeSupervisor(fake_bin, log)
    env_file = tmp_path / ".env"

    def run(env_text: str) -> list[str]:
        env_file.write_text(env_text, encoding="utf-8")
        log.write_text("", encoding="utf-8")
        return _run_entrypoint(fake_bin, env_file, log, tmp_path)

    return run


@pytest.mark.parametrize("env_text,expected", [
    # 单源：只启动所选音源
    ("FNMUSIC_MUSICDL_ENABLED=true\n", ["start musicdl"]),
    ("FNMUSIC_NETEASE_ENABLED=true\n", ["start musicbox"]),
    ("FNMUSIC_LX_ENABLED=true\n", ["start lxserver", "start lxmusic"]),
    # WebUI 独立开关
    ("FNMUSIC_LX_ENABLED=true\nFNMUSIC_WEBUI_ENABLED=true\n", ["start lxserver", "start lxmusic", "start webui"]),
    ("FNMUSIC_WEBUI_ENABLED=true\n", ["start webui"]),
    # 旧多源并存：只取第一个命中（musicdl 优先），不重复拉起
    ("FNMUSIC_MUSICDL_ENABLED=true\nFNMUSIC_NETEASE_ENABLED=true\nFNMUSIC_LX_ENABLED=true\n",
     ["start musicdl"]),
    # 全关：什么源都不启动
    ("FNMUSIC_MUSICDL_ENABLED=false\n", []),
])
def test_entrypoint_starts_selected_programs(entrypoint_env, env_text, expected):
    assert entrypoint_env(env_text) == expected


# ---------------------------------------------------------------- 网易云歌单常驻

def _seed_musicbox_cookie(tmp_path) -> None:
    cookie = tmp_path / "data" / "netease-musicbox" / "cookie.txt"
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text("MUSIC_U=stub;", encoding="utf-8")


def test_entrypoint_resident_starts_musicbox_with_cookie(entrypoint_env, tmp_path):
    """非 musicbox 音源 + 常驻开启 + 盘上有 cookie：拉起 musicbox 供网易账号歌单。"""
    _seed_musicbox_cookie(tmp_path)
    env_text = ("FNMUSIC_MUSICDL_ENABLED=true\n"
                "FNMUSIC_NETEASE_PLAYLIST_RESIDENT=true\n"
                "FNMUSIC_NETEASE_MY_PLAYLISTS=true\n")
    assert entrypoint_env(env_text) == ["start musicdl", "start musicbox"]


def test_entrypoint_resident_skips_without_cookie(entrypoint_env, tmp_path):
    """常驻开启但盘上无 cookie（未登录）：不拉起，登录后由 webui 对账兜底。"""
    env_text = ("FNMUSIC_MUSICDL_ENABLED=true\n"
                "FNMUSIC_NETEASE_PLAYLIST_RESIDENT=true\n"
                "FNMUSIC_NETEASE_MY_PLAYLISTS=true\n")
    assert entrypoint_env(env_text) == ["start musicdl"]


@pytest.mark.parametrize("mapping,needs_musicbox", [
    ({"a": [], "b": [{"provider": "musicbox", "source": "netease",
                     "kind": "chart", "id": "3778678"}]}, True),
    ({"a": [], "b": []}, False),
    ({}, False),
])
def test_entrypoint_public_resident_considers_every_user(entrypoint_env, mapping, needs_musicbox):
    import json
    env_text = ("FNMUSIC_MUSICDL_ENABLED=true\n"
                "FNMUSIC_NETEASE_PLAYLIST_RESIDENT=true\n"
                "FNMUSIC_PUBLIC_PLAYLISTS_ENABLED=true\n"
                "FNMUSIC_PUBLIC_PLAYLISTS_BY_USER='" + json.dumps(mapping) + "'\n")
    assert entrypoint_env(env_text) == ["start musicdl"] + (["start musicbox"] if needs_musicbox else [])


def test_entrypoint_resident_requires_my_playlists_flag(entrypoint_env, tmp_path):
    """常驻为账号歌单供数：账号歌单开关未开时不空跑 musicbox。"""
    _seed_musicbox_cookie(tmp_path)
    env_text = "FNMUSIC_MUSICDL_ENABLED=true\nFNMUSIC_NETEASE_PLAYLIST_RESIDENT=true\n"
    assert entrypoint_env(env_text) == ["start musicdl"]


def test_entrypoint_musicbox_provider_not_duplicated_by_resident(entrypoint_env, tmp_path):
    """musicbox 本就是当前音源：常驻分支不重复 start。"""
    _seed_musicbox_cookie(tmp_path)
    env_text = ("FNMUSIC_NETEASE_ENABLED=true\n"
                "FNMUSIC_NETEASE_PLAYLIST_RESIDENT=true\n"
                "FNMUSIC_NETEASE_MY_PLAYLISTS=true\n")
    assert entrypoint_env(env_text) == ["start musicbox"]


# ---------------------------------------------------------------- compose / Dockerfile

def test_compose_single_service_layout():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert list(compose["services"]) == ["fnmusic-sources"]
    svc = compose["services"]["fnmusic-sources"]
    assert svc["build"]["dockerfile"] == "container/Dockerfile"
    assert svc["image"] == "fnmusic-sources:latest"
    # 音源和管理页都只发布在 127.0.0.1；浏览器走飞牛网关
    assert sorted(svc["ports"]) == sorted([
        "127.0.0.1:8768:8001",
        "127.0.0.1:8770:8002",
        "127.0.0.1:8772:8003",
        "127.0.0.1:8774:8004",
    ])
    raw = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "0.0.0.0:8770" not in raw
    assert "0.0.0.0:8774" not in raw
    assert "./sources-data:/data" in svc["volumes"]
    assert any(v.startswith(".:/repo") for v in svc["volumes"])
    assert svc["restart"] == "unless-stopped"
    assert svc["healthcheck"]["test"] == ["CMD", "/usr/local/bin/healthcheck.sh"]


def test_compose_build_args_mirror_passthrough():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    build = compose["services"]["fnmusic-sources"]["build"]
    args = build["args"]
    for name in ("BASE_IMAGE", "PIP_INDEX_URL", "APT_MIRROR"):
        assert name in args and "${" in args[name], f"构建参数 {name} 需从 .env 透传"
    # issue #71：默认构建不依赖 BuildKit 的 network.host 授权。
    assert build.get("network", "default") == "default"
    assert not build.get("entitlements")


def test_dockerfile_assembly():
    text = (CONTAINER_DIR / "Dockerfile").read_text(encoding="utf-8")
    # 单镜像运行时：python + node(lx 沙箱) + ffmpeg(musicdl 试听) + supervisor
    for pkg in ("nodejs", "ffmpeg", "supervisor"):
        assert pkg in text
    for req in ("musicdl-service/requirements.txt", "musicbox-service/requirements.txt",
                "lxmusic-service/requirements.txt", "webui-service/requirements.txt"):
        assert req in text
    assert "container/lxserver.version" in text
    assert "container/ensure_lxserver_native.sh" in text
    assert "ensure-lxserver-native" in text
    assert "lxmusic-service/lxserver_client.py" in text
    assert "lxmusic-service/js/bridge.js" not in text
    assert text.count("HEALTHCHECK") >= 1
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in text
    assert "EXPOSE 8001 8002 8003 8004" in text
    # 非特权运行
    assert "USER appuser" in text
    # 数据卷与仓库挂载点；目录不可 world-writable（bind 挂载后跟宿主机权限）
    assert "/data" in text and "/repo" in text
    assert "chmod 0777" not in text
    # COPY 保留上下文 mode：umask 077 检出下 supervisord.conf 会以 600 进镜像，
    # appuser 读不了配置导致容器起不来；必须显式规范化权限
    assert "chmod 0644 /etc/supervisor/supervisord.conf" in text


def test_container_logging_root_contract():
    text = (CONTAINER_DIR / "Dockerfile").read_text(encoding="utf-8")
    env_block = text.split("ENV ", 1)[1].split("\n\n", 1)[0]
    assert "FNMUSIC_LOG_DIR=/data/logs" in env_block
    assert "groupadd -g 1000 appuser" in text
    assert "useradd -m -u 1000 -g 1000 appuser" in text
    assert "USER appuser" in text
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["fnmusic-sources"]
    assert ".:/repo" in service["volumes"]
    # Host .env values must not replace the image's container-only log root.
    assert "env_file" not in service
    assert "FNMUSIC_LOG_DIR" not in service.get("environment", {})
    env_lib = (CONTAINER_DIR / "env_flag.sh").read_text()
    exports = env_lib.split("export_source_env()", 1)[1]
    assert "FNMUSIC_LOG_DIR" not in exports
    assert "FNMUSIC_HOME" not in exports


def test_dockerfile_network_fallback_resilience():
    """构建容器网络自愈：始终注入备用公共 DNS；apt 回退看索引落地而非退出码；pip 回退官方源。

    背景：宿主 DNS 指向本机（127.x）时 Docker 构建容器回退 8.8.8.8（国内不可达），
    apt-get update 对 DNS 失败只报 W: 警告且退出码为 0，旧版 `if ! apt-get update`
    的回退永远不触发，最终误报 Unable to locate package。getent 探测本身依赖
    DNS/NSS，缺失或失败还可能被 set -e 打死，因此改为不探测、始终注入。
    """
    text = (CONTAINER_DIR / "Dockerfile").read_text(encoding="utf-8")
    apt_step = text.split("# nodejs（洛雪自定义源")[1].split("# 四个服务的")[0]
    # DNS 自愈：始终注入备用公共 DNS（仅本构建层内生效），不依赖 getent
    assert "getent hosts" not in apt_step
    assert "223.5.5.5" in apt_step and "119.29.29.29" in apt_step
    assert "cat /tmp/resolv.fnmusic" in apt_step
    # 回退触发不能依赖 apt-get update 退出码，必须校验索引真正落地
    assert "if ! apt-get update" not in text
    assert "lists_fetched" in apt_step and "/var/lib/apt/lists" in apt_step
    assert "|| true)" in apt_step  # find 失败不得被 set -e 打死
    # 镜像源与官方源双失败要给出带排查建议的明确错误；禁止 [ERROR] 污染弹窗 grep。
    # 全文件（含注释）不得出现字面 [ERROR]：BuildKit 失败时会带行号回显 Dockerfile
    # 上下文，注释里的字面 [ERROR] 同样会被应用中心弹窗错误摘要误收（issue #109）。
    assert "均未取到索引" in apt_step
    assert "daemon.json" in apt_step
    assert "[ERROR]" not in text
    # 双源失败时必须报告备用 DNS 注入是否真正生效，区分「注入失败」与「源不可达」
    assert "注入未生效" in apt_step
    # pip 步骤：同样自带 DNS 自愈（resolv.conf 改写不跨 RUN 层）+ 官方 PyPI 回退
    pip_step = text.split("# 四个服务的")[1]
    assert "getent hosts" not in pip_step
    assert "223.5.5.5" in pip_step
    assert "https://pypi.org/simple" in pip_step


def test_dockerignore_whitelist_build_context():
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    code_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    # 白名单模式：先忽略一切，再放行四个服务目录与 container/
    assert code_lines[0] == "*"
    for keep in ("!musicdl-service/", "!musicbox-service/", "!lxmusic-service/", "!webui-service/", "!container/"):
        assert keep in code_lines


def _imported_proxy_modules(path: Path, *, relative_to_proxy: bool = False) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "proxy" and len(parts) > 1:
                    modules.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if relative_to_proxy and node.level == 1:
                if node.module:
                    modules.add(node.module.split(".")[0])
                else:
                    modules.update(alias.name for alias in node.names)
            elif node.module:
                parts = node.module.split(".")
                if parts[0] == "proxy" and len(parts) > 1:
                    modules.add(parts[1])
                elif parts[0] == "proxy":
                    modules.update(alias.name for alias in node.names)
    return modules


def test_dockerfile_copies_proxy_modules_imported_by_services():
    """镜像内服务允许 import proxy.*（webui 复用 .env 安全合并）。

    回归：v2.0.0 首次安装时 webui 进程在容器内 ModuleNotFoundError 崩溃——
    Dockerfile 没有 COPY proxy/，.dockerignore 白名单也没放行。
    """
    text = (CONTAINER_DIR / "Dockerfile").read_text(encoding="utf-8")
    copied = " ".join(ln for ln in text.splitlines() if ln.startswith("COPY"))
    ignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    # proxy/app.py 按部署形态动态导入公共歌单模块；正则扫描静态 import 不容易覆盖。
    required = {"publicplaylists"} | set(re.findall(r"\bproxy/([A-Za-z_]\w*)\.py\b", copied))
    origins: dict[str, str] = {"publicplaylists": "动态导入（proxy/app.py）"}
    for service_dir in ("musicdl-service", "musicbox-service", "lxmusic-service", "webui-service"):
        for py in sorted((REPO_ROOT / service_dir).glob("*.py")):
            # 测试文件（test_*/conftest）不进镜像、不随服务运行：其跨目录 import
            # （如 lxmusic 测试比对 proxy 的 UA 常量）不构成镜像依赖
            if py.name.startswith("test_") or py.name == "conftest.py":
                continue
            for module_name in _imported_proxy_modules(py):
                required.add(module_name)
                origins.setdefault(module_name, f"{service_dir}/{py.name}")

    pending = set(required)
    while pending:
        module_name = pending.pop()
        proxy_file = REPO_ROOT / "proxy" / f"{module_name}.py"
        if not proxy_file.is_file():
            continue
        for dependency in _imported_proxy_modules(proxy_file, relative_to_proxy=True):
            if dependency not in required:
                required.add(dependency)
                origins.setdefault(dependency, f"proxy/{module_name}.py")
                pending.add(dependency)

    for module_name in sorted(required):
        module = f"proxy/{module_name}.py"
        origin = origins.get(module_name, "Dockerfile COPY")
        assert module in copied, f"{origin} 依赖 {module}，Dockerfile 需 COPY 进镜像"
        assert f"!{module}" in ignore, f"{origin} 依赖 {module}，.dockerignore 需放行构建上下文"


def test_entrypoint_creates_log_root_before_supervisord(tmp_path):
    """日志根不存在时 supervisord 因 logfile 目录缺失整体启动失败（CI 实证）。

    entrypoint 必须在拉起 supervisord 前先建日志根；Docker 与 native 共用
    此入口，目录取 FNMUSIC_LOG_DIR（容器 /data/logs，native 为仓库 logs）。
    """
    log = tmp_path / "ctl.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    FakeSupervisor(fake_bin, log)
    env_file = tmp_path / ".env"
    env_file.write_text("FNMUSIC_MUSICDL_ENABLED=true\n", encoding="utf-8")
    # 深层不存在的日志根：验证 mkdir -p 语义，而非仅一层目录
    log_root = tmp_path / "repo-root" / "nested" / "logs"
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ENV_FLAG_LIB": str(CONTAINER_DIR / "env_flag.sh"),
        "SUPERVISOR_CONF": "/nonexistent/supervisord.conf",
        "FNMUSIC_ENV_FILE": str(env_file),
        "FNMUSIC_LOG_DIR": str(log_root),
        # 重定向数据目录：宿主 macOS 上 /data 只读，容器外测试必须指到临时目录
        "DATA_PATH": str(tmp_path / "data" / "lxserver"),
        "LX_DATA_DIR": str(tmp_path / "data" / "lxmusic"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    proc = subprocess.run(
        ["sh", str(CONTAINER_DIR / "entrypoint.sh")],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert log_root.is_dir(), "entrypoint 应在 supervisord 启动前创建日志根目录"
