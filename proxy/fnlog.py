"""fnmusic-ext 统一日志模块.

三个进程（proxy 宿主侧 / webui / lxmusic）共用：
- 本地文件日志，按天午夜滚动，backupCount=2 → 恰好保留 3 天（当前 + 2 个轮转）；
  另设单文件 50MB 保险上限，防异常刷屏撑爆磁盘。
- purge_stale() 按 mtime 清理超期文件（覆盖 install.log 等无法自轮转的追加文件
  与历史导出的 .logzip），服务启动时与每日调用。
- 所有文件 IO 失败一律静默降级为仅 stdout，绝不影响业务。
- redact() 对 key/token/secret/password/cookie/authorization 的值打码后再入日志。
- env_snapshot_lines() 采集运行环境（系统/网络/飞牛音乐/扩展配置摘要），
  宿主视角由 proxy 落盘 env_snapshot.txt 供导出；容器视角只记录可见部分。

docker 形态下 logs 目录位于挂载进容器的仓库目录（.:/repo），webui 导出接口
因此能同时读到宿主 proxy 写入的日志。
"""
from __future__ import annotations

import glob
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import time
from pathlib import Path

try:
    from .env_merge import parse_env_file
    from .version import get_version
except ImportError:  # 独立/容器内以 --app-dir 方式加载
    from env_merge import parse_env_file  # type: ignore
    from version import get_version  # type: ignore

RETENTION_DAYS = 3
MAX_FILE_BYTES = 50 * 1024 * 1024
SNAPSHOT_FILE = "env_snapshot.txt"

# 与 takeover.py 的秘密变量名规则保持一致；authorization 头的值含空格，单独兜住
_SECRET_VALUE_RE = re.compile(
    r"\b(?i:(authorization))(\s*[=:]\s*)([^\r\n]{1,160})"
    r"|\b(?i:([A-Za-z0-9_]*(?:key|token|secret|passwd|password|cookie)[A-Za-z0-9_]*))"
    r"(\s*[=:]\s*)(\"[^\"]{0,120}\"|'[^']{0,120}'|[^\s,;\"']{1,120})"
)
_SECRET_ENV_KEYS = re.compile(r"(?i)key|token|secret|password|cookie")

_TRIM_MUSIC_SOCKETS = (
    "/var/run/trim_music.socket",
    "/var/run/trim_music_upstream.socket",
)
_MUSIC_DB = "/usr/local/apps/@appdata/trim.music/db/music.db"
_GATEWAY_CONF = "/usr/trim/etc/network_gateway_setting.conf"


def log_dir() -> Path:
    """日志目录：FNMUSIC_LOG_DIR > FNMUSIC_HOME/logs > 仓库根/logs。"""
    override = (os.environ.get("FNMUSIC_LOG_DIR") or "").strip()
    if override:
        return Path(override)
    home = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if home:
        return Path(home) / "logs"
    here = Path(__file__).resolve()
    for cand in here.parent.parents:
        if (cand / "proxy").is_dir() and (cand / "VERSION").exists():
            return cand / "logs"
    return here.parent.parent / "logs"


class _BoundedTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """按天滚动 + 单文件大小保险双保险。"""

    def __init__(self, *args, max_bytes: int = MAX_FILE_BYTES, **kwargs):
        self._max_bytes = max_bytes
        super().__init__(*args, **kwargs)

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        try:
            if super().shouldRollover(record):
                return True
            if self.stream is None:
                self.stream = self.open()
            msg = "%s\n" % self.format(record)
            return self.stream.tell() + len(msg) > self._max_bytes
        except Exception:  # noqa: BLE001
            return False


_SET_UP: set[str] = set()


def setup_logging(logger_name: str, service: str, level: int = logging.INFO) -> logging.Logger:
    """给服务 logger 挂文件 handler（logs/<service>.log），幂等；失败静默跳过。"""
    lg = logging.getLogger(logger_name)
    key = f"{logger_name}:{service}"
    if key not in _SET_UP:
        _SET_UP.add(key)
        try:
            directory = log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            handler = _BoundedTimedRotatingFileHandler(
                directory / f"{service}.log",
                when="midnight",
                backupCount=RETENTION_DAYS - 1,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            lg.addHandler(handler)
        except Exception:  # noqa: BLE001
            pass
        purge_stale()
    lg.setLevel(level)
    return lg


def purge_stale(days: int = RETENTION_DAYS, now: "float | None" = None) -> list[str]:
    """删除日志目录中 mtime 超过 days 天的文件，返回被删文件名。"""
    removed: list[str] = []
    try:
        directory = log_dir()
        cutoff = (now if now is not None else time.time()) - days * 86400
        for item in directory.iterdir():
            try:
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink()
                    removed.append(item.name)
            except OSError:
                continue
    except OSError:
        pass
    return removed


def redact(text: str) -> str:
    """对疑似秘密的 键=值 / 键: 值 打码（authorization 值整体打码）。"""
    def _mask(m: "re.Match[str]") -> str:
        return f"{m.group(1) or m.group(4)}{m.group(2) or m.group(5)}***"
    return _SECRET_VALUE_RE.sub(_mask, str(text))


def diag(logger: logging.Logger, event: str, **fields: object) -> None:
    """诊断日志：[diag] 事件 k=v …（脱敏 + 截断；与 takeover 白名单的
    `[diag] [ -~]{0,300}` 条目对齐，超长/非 ASCII 行只落文件不进 journal）。"""
    parts = [redact(f"{k}={v}")[:80] for k, v in fields.items()]
    line = " ".join(parts)[:300]
    logger.info("[diag] %s %s", event, line)


def _run(cmd: "list[str]") -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _read_first(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    return line
    except OSError:
        pass
    return ""


def _os_pretty_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _fnos_version() -> str:
    for pattern in ("/usr/trim/version*", "/usr/trim/etc/version*", "/usr/trim/*version*"):
        for cand in sorted(glob.glob(pattern)):
            text = _read_first(cand)
            if text:
                return f"{os.path.basename(cand)}: {text[:80]}"
    return "unknown"


def _ip_addrs() -> list[str]:
    out = _run(["ip", "-o", "-4", "addr", "show", "scope", "global"])
    addrs: list[str] = []
    for line in out.splitlines():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if m:
            addrs.append(m.group(1))
    if not addrs:
        text = _run(["hostname", "-I"]) or _run(["hostname", "-i"])
        addrs = [t for t in text.split() if re.match(r"^\d+\.\d+\.\d+\.\d+$", t)]
    if not addrs:
        try:  # 容器内兜底：UDP connect 不发包也能拿到本机出口地址
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                addrs = [s.getsockname()[0]]
        except OSError:
            pass
    return addrs


def _gateway_ports() -> str:
    try:
        with open(_GATEWAY_CONF, encoding="utf-8") as f:
            text = f.read()
        ports = {"http": "5666", "https": "5667"}

        def scan(obj: object) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = str(k).lower()
                    if isinstance(v, (int, str)) and str(v).isdigit():
                        if "https" in kl and "port" in kl:
                            ports["https"] = str(v)
                        elif "http" in kl and "port" in kl:
                            ports["http"] = str(v)
                    else:
                        scan(v)
            elif isinstance(obj, list):
                for it in obj:
                    scan(it)

        if text.strip():
            scan(json.loads(text))
        return f"{ports['http']}/{ports['https']}"
    except Exception:  # noqa: BLE001
        return "5666/5667(default)"


def _trim_music_state() -> list[str]:
    out = []
    for path in _TRIM_MUSIC_SOCKETS:
        out.append(f"{os.path.basename(path)}={'yes' if os.path.exists(path) else 'no'}")
    out.append(f"music_db={'yes' if os.path.exists(_MUSIC_DB) else 'no'}")
    return out


def _env_summary() -> str:
    env_path = log_dir().parent / ".env"
    try:
        kv_list, _ = parse_env_file(env_path)
    except Exception:  # noqa: BLE001
        return ""
    items = []
    for k, v in kv_list:
        if _SECRET_ENV_KEYS.search(k):
            continue
        value = str(v).strip()
        if value:
            items.append(f"{k}={redact(f'{k}={value}').split('=', 1)[-1][:60]}")
    return " ".join(items)[:600]


def env_snapshot_lines(context: str = "host") -> list[str]:
    """运行环境快照（每行带 [env] 前缀）。容器上下文自动跳过宿主专属探测。"""
    deploy = os.environ.get("FNMUSIC_DEPLOY_MODE", "") or ""
    lines = [f"[env] context={context} app_version={get_version()} deploy_mode={deploy or 'unknown'}"]
    lines.append(f"[env] os={_os_pretty_name() or (_run(['uname', '-sr']) or 'unknown')}")
    if context == "host":
        kernel = _run(["uname", "-a"])
        if kernel:
            lines.append(f"[env] kernel={kernel[:160]}")
        lines.append(f"[env] fnos_version={_fnos_version()}")
    lines.append(f"[env] hostname={socket.gethostname()}")
    addrs = _ip_addrs()
    lines.append(f"[env] addrs={' '.join(addrs) if addrs else 'unknown'}")
    if context == "host":
        lines.append(f"[env] fnos_gateway_ports={_gateway_ports()}")
        lines.append(f"[env] trim_music={' '.join(_trim_music_state())}")
        summary = _env_summary()
        if summary:
            lines.append(f"[env] config {summary}")
    else:
        lines.append("[env] note=容器视角，宿主系统/网络信息见 proxy 侧 env_snapshot.txt")
    return lines


def write_env_snapshot(context: str = "host") -> bool:
    """把环境快照写入 logs/env_snapshot.txt（proxy 启动时调用，host 视角）。"""
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        header = (
            f"fnmusic-ext 环境快照  生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "-" * 60 + "\n"
        )
        (directory / SNAPSHOT_FILE).write_text(
            header + "\n".join(env_snapshot_lines(context)) + "\n", encoding="utf-8"
        )
        return True
    except Exception:  # noqa: BLE001
        return False
