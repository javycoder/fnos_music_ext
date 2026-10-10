"""fnmusic-ext 统一日志模块.

三个进程（proxy 宿主侧 / webui / lxmusic）共用：
- 本地文件日志，按天午夜或单文件 50MB 滚动；最多保留当前 + 2 个轮转，
  同一天多次大小轮转也不覆盖已有文件，且不会无限积累。
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
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

try:
    from .env_merge import parse_env_file
    from .version import get_version
except ImportError:  # 独立/容器内以 --app-dir 方式加载
    from env_merge import parse_env_file  # type: ignore
    from version import get_version  # type: ignore

RETENTION_DAYS = 3
MAX_FILE_BYTES = 50 * 1024 * 1024
SNAPSHOT_FILE = "env_snapshot.txt"

# Match assignments, including JSON/Python quoted keys and URL query parameters.
# Values are scanned below: length-limited regexes leak the tail of long secrets.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?<![\w.-])(?P<quote>[\"']?)(?P<key>[\w.-]*"
    r"(?:authorization|key|token|secret|passwd|password|cookie)[\w.-]*)"
    r"(?P=quote)(?P<sep>\s*[=:]\s*)", re.IGNORECASE
)
_SECRET_ENV_KEYS = re.compile(r"(?i)authorization|key|token|secret|passwd|password|cookie")

_TRIM_MUSIC_SOCKETS = (
    "/var/run/trim_music.socket",
    "/var/run/trim_music_upstream.socket",
)
_MUSIC_DB = "/usr/local/apps/@appdata/trim.music/db/music.db"
_GATEWAY_CONF = "/usr/trim/etc/network_gateway_setting.conf"


def log_dir() -> Path:
    """FNMUSIC_LOG_DIR > FNMUSIC_HOME/logs > WEBUI_REPO_DIR/logs > 仓库/logs。"""
    override = (os.environ.get("FNMUSIC_LOG_DIR") or "").strip()
    if override:
        return Path(override)
    home = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if home:
        return Path(home) / "logs"
    repo = (os.environ.get("WEBUI_REPO_DIR") or "").strip()
    if repo:
        return Path(repo) / "logs"
    here = Path(__file__).resolve()
    for cand in here.parent.parents:
        if (cand / "proxy").is_dir() and (cand / "VERSION").exists():
            return cand / "logs"
    # Images copy fnlog into /app, whereas the shared repository is mounted /repo.
    return Path("/repo/logs")


def _open_log_file(filename: str | Path, mode: str = "a", encoding: str = "utf-8",
                   errors: str | None = None):
    """Open a regular, single-link log safely; only chmod files owned by this uid.

    O_EXCL identifies newly created files, initially 0600 even with a lax umask.
    fchmod(0640) defeats root's umask 0077 without changing process-wide umask.
    The setgid logs directory supplies the group; never chown existing paths or
    follow symlinks/hardlinks (especially when called by the host root process).
    Never change a foreign-owned file's permissions; only accept one already
    at 0640 if the OS permits opening it. Insecure foreign-owned files are rejected.
    """
    flags = os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    if mode == "a":
        flags |= os.O_APPEND
    elif mode != "w":
        raise ValueError("log files support only append or write mode")
    try:
        fd = os.open(filename, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(filename, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("log path must be a regular file with one link")
        if info.st_uid == os.geteuid():
            os.fchmod(fd, 0o640)
        elif stat.S_IMODE(info.st_mode) != 0o640:
            raise PermissionError("refusing to change a foreign-owned log file")
        if mode == "w":
            os.ftruncate(fd, 0)
        return os.fdopen(fd, mode, encoding=encoding, errors=errors)
    except BaseException:
        os.close(fd)
        raise


class _BoundedTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Daily + byte-size rollover, with a bounded, collision-free archive set.

    Own the rollover rather than delegating to TimedRotatingFileHandler: 3.11
    deletes a same-day destination, while 3.13 returns early and leaves the
    active file growing. Size rollovers must not postpone the midnight timer.
    """

    def __init__(self, *args, max_bytes: int = MAX_FILE_BYTES, **kwargs):
        self._max_bytes = max_bytes
        super().__init__(*args, **kwargs)

    def _open(self):
        return _open_log_file(self.baseFilename, self.mode, self.encoding, self.errors)

    def computeRollover(self, current_time):  # noqa: N802
        if self.when != "MIDNIGHT":
            return super().computeRollover(current_time)
        # Calendar days keep creation and later rollovers at midnight across
        # DST on both versions (3.11 lacks the fixes in 3.13's implementation).
        tz = timezone.utc if self.utc else None
        today = datetime.fromtimestamp(current_time, tz).date()
        at = self.atTime or datetime.min.time()
        candidate = datetime.combine(today, at, tzinfo=tz)
        if candidate.timestamp() <= current_time:
            candidate += timedelta(days=1)
        candidate += timedelta(days=max(0, self.interval // 86400 - 1))
        return int(candidate.timestamp())

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        if super().shouldRollover(record):
            return True
        if self._max_bytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()
        size = os.fstat(self.stream.fileno()).st_size
        msg = self.format(record) + self.terminator
        # A single oversized record is unavoidable; do not rotate an empty file.
        return size > 0 and size + len(msg.encode(self.stream.encoding, self.stream.errors)) > self._max_bytes

    def _archives(self) -> list[Path]:
        base = Path(self.baseFilename)
        date_pattern = re.escape(self.suffix)
        for directive, digits in (("%Y", 4), ("%m", 2), ("%d", 2), ("%H", 2), ("%M", 2), ("%S", 2)):
            date_pattern = date_pattern.replace(directive, rf"\d{{{digits}}}")
        pattern = re.compile(re.escape(base.name) + r"\." + date_pattern
                             + r"(?:\.\d+)?$", re.ASCII)
        return sorted(p for p in base.parent.iterdir() if pattern.fullmatch(p.name))

    def getFilesToDelete(self) -> list[str]:  # noqa: N802
        files = self._archives()
        # Unlike the stdlib's backupCount=0 (unlimited), zero means no archives.
        return [str(p) for p in files[:max(0, len(files) - self.backupCount)]]

    def doRollover(self) -> None:  # noqa: N802
        current_time = int(time.time())
        timed = current_time >= self.rolloverAt
        stamp = time.strftime(self.suffix, time.gmtime(self.rolloverAt - self.interval)
                              if self.utc else time.localtime(self.rolloverAt - self.interval))
        destination = self.baseFilename + "." + stamp
        # Fixed-width suffixes sort in creation order and survive process restart.
        sequences = [int(p.name.rsplit(".", 1)[-1]) for p in self._archives()
                     if str(p).startswith(destination + ".") and p.name.rsplit(".", 1)[-1].isdigit()]
        sequence = max(sequences, default=0) + 1
        destination = self.rotation_filename(f"{destination}.{sequence:08d}")
        if self.stream:
            self.stream.close()
            self.stream = None
        self.rotate(self.baseFilename, destination)
        for filename in self.getFilesToDelete():
            os.remove(filename)
        if not self.delay:
            self.stream = self._open()
        if timed:
            next_time = self.computeRollover(current_time)
            while next_time <= current_time:
                next_time += self.interval
            self.rolloverAt = next_time

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # Disk full, permissions, or a vanished mount must not affect business or
        # print an unredacted record in logging's default stderr diagnostic.
        pass


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


def _secret_value_end(text: str, start: int, key: str, quoted_key: bool) -> int:
    if start >= len(text):
        return start
    is_header = not quoted_key and re.search(r"authorization|cookie", key, re.IGNORECASE)
    if text[start] in "\"'[{":
        # Quoted strings may contain escapes, commas, spaces and URL tokens;
        # JSON secret objects/arrays must be masked in their entirety as well.
        stack = [text[start]]
        end = start + 1
        while end < len(text) and stack:
            char = text[end]
            if stack[-1] in "\"'":
                if char == "\\":
                    end += 2
                    continue
                if char == stack[-1]:
                    stack.pop()
            elif char in "\"'[{":
                stack.append(char)
            elif char == {"[": "]", "{": "}"}.get(stack[-1]):
                stack.pop()
            end += 1
        # A quoted auth/cookie assignment may still have additional unquoted
        # header values after it; cover that tail, not just the first quote.
        if not is_header:
            return min(end, len(text))
        if end >= len(text):
            return len(text)
        start = end
    if is_header:
        newline = re.search(r"[\r\n]", text[start:])
        return start + newline.start() if newline else len(text)
    end = start
    while end < len(text) and text[end] not in " \t\r\n,;\"'&{}[]":
        end += 1
    return end


def redact(text: str) -> str:
    """Mask complete secret assignments, headers, JSON values and URL tokens.

    Decode percent-encoded nested URL parameters only when the decoded form
    contains a secret key; normal diagnostic URLs retain their original form.
    """
    def mask_encoded(match: re.Match[str]) -> str:
        original = decoded = match.group()
        # Each successful decode shortens the input, so this also handles URLs
        # nested more than an arbitrary fixed number of encoding levels.
        while True:
            next_text = unquote(decoded)
            if next_text == decoded:
                break
            decoded = next_text
        if decoded != original and _SECRET_ASSIGNMENT_RE.search(decoded):
            # Do not emit decoded secret tails containing %20/%26/etc. Mask the
            # whole URL fragment rather than guessing its encoded boundaries.
            return "***"
        return original

    text = re.sub(r"[^\s\"'<>]+", mask_encoded, str(text))
    parts = []
    position = 0
    while match := _SECRET_ASSIGNMENT_RE.search(text, position):
        parts.append(text[position:match.end()])
        parts.append("***")
        position = _secret_value_end(text, match.end(), match.group("key"), bool(match.group("quote")))
    parts.append(text[position:])
    return "".join(parts)


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
        with _open_log_file(directory / SNAPSHOT_FILE, "w") as snapshot:
            snapshot.write(redact(header + "\n".join(env_snapshot_lines(context)) + "\n"))
        return True
    except Exception:  # noqa: BLE001
        return False
