"""#108 运行环境：lxserver 有界滚动日志与分段健康的离线测试。

覆盖三条已经证实的诊断缺口：

1. 容器与 native 形态下 lxserver 的 stdout/ stderr 不落盘到共享日志根，
   于是管理页导出的日志 zip 里永远没有 lxserver 运行记录（附件 zip 已证实）；
2. 健康检查只看 supervisor 进程状态 RUNNING，进程存活 ≠ HTTP 就绪
   （#108 现象：两个进程都 RUNNING，lxmusic 查询 lxserver 持续 TimeoutError）；
3. 慢初始化缺少有界 deadline，失败缺少分层归因线索。

本文件只做离线契约与脚本行为断言，不连接 NAS、不起容器。
"""
from __future__ import annotations

import configparser
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

CONTAINER_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONTAINER_DIR.parent
SUPERVISORD_CONF = CONTAINER_DIR / "supervisord.conf"
NATIVE_CONF_IN = CONTAINER_DIR / "supervisord-native.conf.in"
HEALTHCHECK = CONTAINER_DIR / "healthcheck.sh"
ENTRYPOINT = CONTAINER_DIR / "entrypoint.sh"

# fnmusic 各服务的进程日志仍然汇/docker logs（stdout），唯独 lxserver 需要独立
# 落盘供导出；这几个进程必须保持原状。
STDOUT_PROGRAMS = ("musicdl", "musicbox", "lxmusic", "webui")


@pytest.fixture(scope="module")
def raw_conf():
    """原始读取：不得触发 configparser 的 %() 插值。"""
    cp = configparser.RawConfigParser()
    cp.read(SUPERVISORD_CONF)
    return cp


# ------------------------------------------------------------- lxserver 日志 --

def test_lxserver_log_file_lands_in_shared_log_root(raw_conf):
    """lxserver 日志必须落在共享日志根，才能进入统一导出包。

    容器形态共享日志根由 Dockerfile 的 ENV 固定为 /data/logs（见
    test_container.py 的 FNMUSIC_LOG_DIR 契约），所有服务进程都往这里写，
    webui 的 /api/logs/export 扫描同一目录。
    """
    section = "program:lxserver"
    logfile = raw_conf.get(section, "stdout_logfile")
    assert logfile.startswith("/data/logs/"), (
        f"lxserver 日志需落在共享日志根 /data/logs/，实际 {logfile}"
    )
    assert logfile.endswith("lxserver.log")
    # 容器 stdout 是 docker logs 单向管道，导出读不到；不能再指回 /dev/stdout
    assert logfile != "/dev/stdout"


def test_lxserver_log_rotation_is_bounded(raw_conf):
    """有界滚动：单文件上限有限、备份份数有限，避免 NAS 磁盘被日志写满。"""
    section = "program:lxserver"
    max_bytes = _parse_size(raw_conf.get(section, "stdout_logfile_maxbytes"))
    backups = int(raw_conf.get(section, "stdout_logfile_backups"))
    assert 0 < max_bytes <= 32 * 1024 * 1024, f"单文件大小上限需在 0~32MB 之间：{max_bytes}"
    assert 1 <= backups <= 3, f"备份份数需有限且不超过 3：{backups}"


def test_other_programs_still_stream_to_container_stdout(raw_conf):
    """除 lxserver 外的服务保持 /dev/stdout：docker logs 聚合仍然可用。"""
    for prog in STDOUT_PROGRAMS:
        section = f"program:{prog}"
        assert raw_conf.get(section, "stdout_logfile") == "/dev/stdout", prog
        assert raw_conf.get(section, "stdout_logfile_maxbytes") == "0", prog


def test_native_lxserver_log_uses_repo_log_root():
    """native 形态：lxserver 日志同样要落在共享日志根（仓库 logs/）。

    native 下过去写到 @RUN@/lxserver.log（sources-native/ 运行目录），
    webui 的导出扫描 log_dir()（仓库 logs/），两边不是同一个目录，
    于是 native 用户的导出包里同样没有 lxserver 记录。
    """
    text = NATIVE_CONF_IN.read_text(encoding="utf-8")
    block = _program_block(text, "lxserver")
    logfile = _field(block, "stdout_logfile")
    assert logfile == "@REPO@/logs/lxserver.log", (
        f"native lxserver 日志需写在 @REPO@/logs/，实际 {logfile}"
    )
    assert _field(block, "stdout_logfile_maxbytes") not in ("0", ""), "native 日志也需有界滚动"
    backups = _field(block, "stdout_logfile_backups")
    assert backups.isdigit() and 1 <= int(backups) <= 3, backups


def test_native_rendered_lxserver_log_matches_log_dir(tmp_path):
    """渲染后的 native 配置里 lxserver 日志路径 == fnlog.log_dir() 的位置。"""
    common = (REPO_ROOT / "proxy" / "install_common.sh").read_text(encoding="utf-8")
    names = ("native_venv_dir", "native_run_dir", "native_sup_conf",
             "render_native_placeholders", "render_native_supervisor_conf")
    func = "".join(_bash_func(common, n) for n in names)
    (tmp_path / "container").mkdir()
    (tmp_path / "container" / "supervisord-native.conf.in").write_text(
        NATIVE_CONF_IN.read_text(encoding="utf-8"), encoding="utf-8")
    script = f'{func}\nrender_native_supervisor_conf && cat "$(native_sup_conf)"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "BASE_DIR": str(tmp_path)},
                         timeout=30)
    assert out.returncode == 0, out.stderr
    rendered = out.stdout
    assert f"stdout_logfile={tmp_path}/logs/lxserver.log" in rendered
    # 占位符必须全部渲染掉，不能留下 @RUN@ 之类的裸占位符
    assert "@REPO@" not in rendered and "@RUN@" not in rendered


# ------------------------------------------------------------- 分段健康检查 --

def test_healthcheck_probes_http_readiness_per_program():
    """健康检查要区分「进程在」与「HTTP 就绪」。

    #108 的现象是 supervisor 两进程 RUNNING，但 HTTP 查询持续 TimeoutError；
    只看进程状态的健康检查会把这种状态一直报成健康。
    """
    text = HEALTHCHECK.read_text(encoding="utf-8")
    assert "http_ready" in text, "healthcheck 需提供 HTTP 就绪探活函数"
    # 进程层与 HTTP 层分别出结论，不能只有一个
    assert "HTTP 未就绪" in text
    for probe_url in ("8003", "8005"):
        assert probe_url in text, f"需探活 {probe_url} 端口对应的服务"
    # 探活必须区分状态码与连接/超时错误，便于归因
    assert "error_type" in text or "kind=" in text


def test_healthcheck_readiness_probe_is_bounded():
    """探活必须有超时上限，健康检查不能在故障时被拖到无限等待。"""
    text = HEALTHCHECK.read_text(encoding="utf-8")
    timeouts = [float(m) for m in re.findall(r"HEALTH_HTTP_TIMEOUT[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)",
                                             text)]
    assert timeouts, "需定义可读的 HTTP 探活超时常量"
    assert all(0 < t <= 10 for t in timeouts), f"探活超时需在 10 秒内：{timeouts}"


def test_healthcheck_lx_webui_layer_uses_supervisor_state_first():
    """进程层结论仍来自 supervisor：未运行时不伪装成 HTTP 故障。"""
    text = HEALTHCHECK.read_text(encoding="utf-8")
    assert "supervisorctl" in text
    assert "应运行但状态异常" in text


def test_healthcheck_runs_with_fake_supervisor(tmp_path):
    """真实跑一遍：进程 RUNNING 但 HTTP 端口空 → 明确报 HTTP 未就绪且退出非 0。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ctl.log"
    (bindir / "supervisorctl").write_text(
        textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "{log}"
        echo "lxserver                        RUNNING   pid 1, uptime 0:01:00"
        echo "lxmusic                         RUNNING   pid 2, uptime 0:01:00"
        exit 0
        """), encoding="utf-8")
    (bindir / "supervisorctl").chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("FNMUSIC_LX_ENABLED='true'\nFNMUSIC_WEBUI_ENABLED='false'\n", encoding="utf-8")
    # 端口指向确定不可达的本机端口，避免依赖真实服务
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ENV_FLAG_LIB": str(CONTAINER_DIR / "env_flag.sh"),
        "FNMUSIC_ENV_FILE": str(env_file),
        "SUPERVISOR_CONF": "/nonexistent/supervisord.conf",
        "LX_MUSIC_HEALTH_URL": "http://127.0.0.1:1/healthz",
        "LX_SERVER_HEALTH_URL": "http://127.0.0.1:1/api/status",
        "HEALTH_HTTP_TIMEOUT": "1",
    }
    proc = subprocess.run(["sh", str(HEALTHCHECK)], capture_output=True, text=True,
                          env=env, timeout=60)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "HTTP 未就绪" in combined, combined
    # 进程层结论仍然正确，不被 HTTP 层结论覆盖
    assert combined.count("应运行但状态异常") == 0, combined


def test_healthcheck_passes_when_both_layers_ready(tmp_path):
    """进程 RUNNING 且 HTTP 有应答 → 健康（退出 0）。"""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静默
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "supervisorctl").write_text(
            "#!/bin/sh\n"
            'echo "lxserver                        RUNNING   pid 1, uptime 0:01:00"\n'
            'echo "lxmusic                         RUNNING   pid 2, uptime 0:01:00"\n',
            encoding="utf-8")
        (bindir / "supervisorctl").chmod(0o755)
        env_file = tmp_path / ".env"
        env_file.write_text("FNMUSIC_LX_ENABLED='true'\nFNMUSIC_WEBUI_ENABLED='false'\n",
                            encoding="utf-8")
        env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "ENV_FLAG_LIB": str(CONTAINER_DIR / "env_flag.sh"),
            "FNMUSIC_ENV_FILE": str(env_file),
            "SUPERVISOR_CONF": "/nonexistent/supervisord.conf",
            "LX_MUSIC_HEALTH_URL": f"http://127.0.0.1:{port}/healthz",
            "LX_SERVER_HEALTH_URL": f"http://127.0.0.1:{port}/api/status",
            "HEALTH_HTTP_TIMEOUT": "2",
        }
        proc = subprocess.run(["sh", str(HEALTHCHECK)], capture_output=True, text=True,
                              env=env, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------- 慢初始化与不清源 --

def test_entrypoint_readiness_wait_is_bounded_and_skipped_by_default(tmp_path):
    """入口的有界等待：默认 0 秒立即返回；设为 N 秒时到点即返回且不动进程。

    关键是「等待期间不清源、不重启」——这是 #108 明确要求的约束。此处用
    真实 shell 执行入口脚本，超时降到 3 秒避免测试拖长。
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ctl.log"
    log.write_text("", encoding="utf-8")
    (bindir / "supervisorctl").write_text(
        textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "{log}"
        case "$1" in
          pid) exit 0 ;;
          *) exit 0 ;;
        esac
        """), encoding="utf-8")
    (bindir / "supervisorctl").chmod(0o755)
    (bindir / "supervisord").write_text("#!/bin/sh\nsleep 1\nexit 0\n", encoding="utf-8")
    (bindir / "supervisord").chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("FNMUSIC_LX_ENABLED='true'\nFNMUSIC_WEBUI_ENABLED='false'\n",
                        encoding="utf-8")
    base_env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ENV_FLAG_LIB": str(CONTAINER_DIR / "env_flag.sh"),
        "FNMUSIC_ENV_FILE": str(env_file),
        "SUPERVISOR_CONF": "/nonexistent/supervisord.conf",
        "DATA_PATH": str(tmp_path / "data" / "lxserver"),
        "LX_DATA_DIR": str(tmp_path / "data" / "lxmusic"),
        # 日志根同样隔离：容器内默认 /data/logs，宿主 runner 上该路径不可建
        "FNMUSIC_LOG_DIR": str(tmp_path / "logs"),
        # 指向确定不可达的端口：等待必然走到超时分支
        "LXMUSIC_HEALTH_URL": "http://127.0.0.1:1/healthz",
    }

    conf_prefix = "-c /nonexistent/supervisord.conf "

    def recorded_calls():
        """读取 supervisorctl 调用记录，排除套接字就绪轮询的 pid。"""
        return [ln.replace(conf_prefix, "")
                for ln in log.read_text(encoding="utf-8").splitlines()
                if ln != conf_prefix + "pid"]

    # 1) 默认关闭：不阻塞，启动动作照旧
    import time as _time
    started = _time.monotonic()
    proc = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True,
                          env=base_env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert _time.monotonic() - started < 15, "默认形态不应阻塞在就绪等待"
    assert recorded_calls() == ["start lxserver", "start lxmusic"], recorded_calls()

    # 2) 显式开启 3 秒预算：到点返回，且不会重启或停止进程
    log.write_text("", encoding="utf-8")
    env = dict(base_env, LX_READY_WAIT="3")
    started = _time.monotonic()
    proc = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True,
                          env=env, timeout=30)
    elapsed = _time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 20, f"就绪等待必须受预算约束，实际 {elapsed:.1f}s"
    combined = proc.stdout + proc.stderr
    assert "未通过 HTTP 就绪检查" in combined, combined
    # 等待失败后的关键约束：只记录，不重启、不停用、不清源
    calls = recorded_calls()
    assert calls == ["start lxserver", "start lxmusic"], (
        f"就绪等待不得重新拉起或停止进程，实际调用：{calls}")
    assert combined.count("start") >= 2


def test_scripts_never_delete_user_sources():
    """容器脚本不得删除用户音源目录：#108 排障期间不能清用户的源。"""
    dangerous = []
    for script in (ENTRYPOINT, HEALTHCHECK, CONTAINER_DIR / "ensure_lxserver_native.sh"):
        text = script.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"rm\s+-rf", stripped) and re.search(r"users/source|uploads", stripped):
                dangerous.append(f"{script.name}: {stripped}")
    assert not dangerous, f"脚本存在删除用户音源的风险命令：{dangerous}"


def test_supervisord_lxserver_has_start_grace():
    """lxserver 进程需要启动宽限：慢初始化不能立刻被判 FATAL 反复重启。"""
    raw = SUPERVISORD_CONF.read_text(encoding="utf-8")
    block = _program_block(raw, "lxserver")
    startsecs = _field(block, "startsecs")
    assert startsecs == "" or int(startsecs) >= 1, startsecs
    stopwait = _field(block, "stopwaitsecs")
    assert stopwait == "" or int(stopwait) >= 1, stopwait


# ------------------------------------------------------------------ 工具函数 --

def _parse_size(value: str) -> int:
    value = value.strip().upper()
    match = re.fullmatch(r"(\d+)(KB|MB|GB|B)?", value)
    assert match, f"无法解析 supervisor 日志大小：{value}"
    number = int(match.group(1))
    unit = match.group(2) or "B"
    return number * {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[unit]


def _program_block(text: str, program: str) -> str:
    start = text.index(f"[program:{program}]")
    rest = text[start + 1:]
    for section in rest.split("\n["):
        candidate = ("[" if not section.startswith("[") else "") + section
        if candidate.startswith(f"[program:{program}]"):
            return candidate
    raise AssertionError(program)


def _field(block: str, key: str) -> str:
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return ""


def _bash_func(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1] + "\n"
    raise AssertionError(name)
