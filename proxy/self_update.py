"""Self-update management: target resolution, streaming download, verification,
and native fnOS App Center upgrade orchestration.

Ensures resilient execution, data preservation, and comprehensive failure reporting.
"""
from __future__ import annotations

import dataclasses
import hashlib
import http.client
import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from proxy.update_release import (
    formal_version,
    normalize_arch,
    normalize_release,
    package_version,
    select_release_assets,
)
from proxy.version import get_version, read_release_channel, ui_version

DEFAULT_GITHUB_REPO = "javycoder/fnos_music_ext"
DEFAULT_GITEE_REPO = "javycoder/fnos_music_ext"
DAEMON_SOCKET = "/var/run/com.trim.app.center.sock"
FALLBACK_FAIL_MESSAGE = "下载失败，请到 GitHub 下载最新版本。"
GITHUB_RELEASE_PAGE = "https://github.com/javycoder/fnos_music_ext/releases"
logger = logging.getLogger(__name__)

DATA_BACKUP_ITEMS = (
    ".env",
    ".env.bak*",
    "config",
    "sources-data",
    "musicbox-data",
    "online_favorites",
    "online_favorites.json",
    "play_history",
    "playlist_tracks",
    "album_registry.json",
    "bind_registry.json",
    "llm_usage.json",
)

_TARGET_CACHE: dict[str, TargetSnapshot] = {}
_ACTIVE_JOB: UpdateJob | None = None
_JOB_HISTORY: list[dict[str, Any]] = []
_UPDATE_LOCK = threading.Lock()


@dataclasses.dataclass
class TargetSnapshot:
    target_id: str
    tag: str
    formal_base: str
    install_version: str
    architecture: str
    asset_name: str
    download_url: str
    size: int | None
    sha256: str
    sha256_url: str
    source: str
    candidates: list[dict[str, Any]]
    created_at: float
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        return current > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "tag": self.tag,
            "formal_base": self.formal_base,
            "install_version": self.install_version,
            "architecture": self.architecture,
            "asset_name": self.asset_name,
            "download_url": self.download_url,
            "size": self.size,
            "sha256": self.sha256,
            "sha256_url": self.sha256_url,
            "source": self.source,
            "candidates": self.candidates,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclasses.dataclass
class UpdateJob:
    id: str
    target_id: str
    tag: str
    install_version: str
    architecture: str
    status: str  # queued | running | completed | failed | unknown
    phase: str   # downloading | verifying | backing_up | staging | installing | restarting | done
    source: str
    downloaded_bytes: int
    total_bytes: int
    daemon_task_id: str
    error_code: str
    message: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_id": self.target_id,
            "tag": self.tag,
            "install_version": self.install_version,
            "architecture": self.architecture,
            "status": self.status,
            "phase": self.phase,
            "source": self.source,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "daemon_task_id": self.daemon_task_id,
            "error_code": self.error_code,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def get_job_base_dir() -> Path:
    """Determine safe storage directory for update jobs and data backups."""
    candidates = [
        Path("/var/apps/fnmusic-ext/var/update_jobs"),
        Path("/vol2/@appdata/fnmusic-ext/update_jobs"),
        Path("/vol1/@appdata/fnmusic-ext/update_jobs"),
    ]
    for cand in candidates:
        parent = cand.parent
        if parent.is_dir():
            try:
                cand.mkdir(parents=True, exist_ok=True)
                return cand
            except OSError:
                pass
    fallback = Path(tempfile.gettempdir()) / "fnmusic-update-jobs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _save_job_state(job_dir: Path, job: UpdateJob) -> None:
    """Atomically persist job state to job.json."""
    job.updated_at = time.time()
    target = job_dir / "job.json"
    tmp = job_dir / f"job.json.{secrets.token_hex(4)}.tmp"
    tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def _read_job_state(job_dir: Path) -> UpdateJob | None:
    target = job_dir / "job.json"
    if not target.is_file():
        return None
    try:
        d = json.loads(target.read_text(encoding="utf-8"))
        return UpdateJob(**d)
    except Exception:
        return None


def get_capability() -> dict[str, Any]:
    """Check whether the environment supports automated download and native upgrade."""
    has_socket = os.path.exists(DAEMON_SOCKET)
    return {
        "allowed": True,
        "native_upgrade": has_socket,
        "platform_machine": platform.machine(),
        "target_arch": normalize_arch(),
    }


def resolve_target(
    tag: str,
    arch: str | None = None,
    http_getter: Callable[[str], dict[str, Any] | None] | None = None,
) -> TargetSnapshot | None:
    """Fetch and align release metadata from Gitee and GitHub into an immutable target snapshot."""
    base = formal_version(tag)
    if not base:
        return None
    tag_str = f"v{base}"
    target_arch = normalize_arch(arch)

    def default_http_get(url: str) -> dict[str, Any] | None:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "fnmusic-ext-self-update", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        return None

    fetch_json = http_getter or default_http_get

    # Priority: 1. Gitee, 2. GitHub
    gitee_url = f"https://gitee.com/api/v5/repos/{DEFAULT_GITEE_REPO}/releases/tags/{tag_str}"
    github_url = f"https://api.github.com/repos/{DEFAULT_GITHUB_REPO}/releases/tags/{tag_str}"

    gitee_raw = fetch_json(gitee_url)
    github_raw = fetch_json(github_url)

    gitee_norm = normalize_release(gitee_raw, "gitee") if gitee_raw else None
    github_norm = normalize_release(github_raw, "github") if github_raw else None

    gitee_asset = select_release_assets(gitee_norm, target_arch) if gitee_norm else None
    github_asset = select_release_assets(github_norm, target_arch) if github_norm else None

    candidates: list[dict[str, Any]] = []
    if gitee_asset and gitee_asset["formal_base"] == base:
        candidates.append(gitee_asset)
    if github_asset and github_asset["formal_base"] == base:
        candidates.append(github_asset)

    if not candidates:
        return None

    primary = candidates[0]
    candidates = [
        candidate for candidate in candidates
        if candidate["install_version"] == primary["install_version"]
        and candidate["asset_name"] == primary["asset_name"]
    ]
    install_ver = primary["install_version"]
    asset_name = primary["asset_name"]

    # Ensure candidate sha256 is reconciled if one source has it
    sha256 = primary.get("sha256") or ""
    if not sha256:
        for c in candidates:
            if c.get("sha256"):
                sha256 = c["sha256"]
                break

    now = time.time()
    target_id = secrets.token_hex(16)
    snapshot = TargetSnapshot(
        target_id=target_id,
        tag=tag_str,
        formal_base=base,
        install_version=install_ver,
        architecture=target_arch,
        asset_name=asset_name,
        download_url=primary["download_url"],
        size=primary.get("size"),
        sha256=sha256,
        sha256_url=primary.get("sha256_url") or "",
        source=primary["source"],
        candidates=candidates,
        created_at=now,
        expires_at=now + 600.0,  # 10 minutes TTL
    )
    _TARGET_CACHE[target_id] = snapshot
    return snapshot


def get_target_snapshot(target_id: str) -> TargetSnapshot | None:
    snap = _TARGET_CACHE.get(target_id)
    if snap and not snap.is_expired():
        return snap
    return None


def _download_and_verify(
    candidate: dict[str, Any],
    target: TargetSnapshot,
    dest_path: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[bool, str]:
    """Download FPK file with incremental SHA256 computation and integrity checks."""
    url = candidate.get("download_url") or ""
    if not url:
        return False, "下载链接为空"

    part_path = dest_path.with_name(f"{dest_path.name}.{secrets.token_hex(4)}.part")
    hasher = hashlib.sha256()
    downloaded = 0
    expected_size = candidate.get("size")
    expected_sha256 = candidate.get("sha256") or target.sha256

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "fnmusic-ext-self-update",
            "Accept": "application/octet-stream,*/*",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            content_length = resp.headers.get("Content-Length")
            total_size = int(content_length) if content_length and content_length.isdigit() else (expected_size or 0)

            with open(part_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total_size)
    except Exception as exc:
        if part_path.exists():
            part_path.unlink()
        return False, f"下载网络异常: {exc}"

    if (expected_size and downloaded != expected_size) or (total_size and downloaded != total_size):
        if part_path.exists():
            part_path.unlink()
        return False, f"文件大小不符（期望 {expected_size or total_size}，实际 {downloaded}）"

    computed_sha = hasher.hexdigest().lower()

    # If sha256 is not known directly, try fetching companion .sha256 file
    sha256_url = candidate.get("sha256_url") or target.sha256_url
    if not expected_sha256 and sha256_url:
        try:
            s_req = urllib.request.Request(
                sha256_url,
                headers={"User-Agent": "fnmusic-ext-self-update"},
            )
            with urllib.request.urlopen(s_req, timeout=10.0) as s_resp:
                if s_resp.status == 200:
                    s_text = s_resp.read().decode("utf-8").strip()
                    m = re.search(r"([0-9a-fA-F]{64})", s_text)
                    if m:
                        expected_sha256 = m.group(1).lower()
        except Exception:
            pass

    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
        if part_path.exists():
            part_path.unlink()
        return False, "缺少有效的 SHA256 校验和"
    if computed_sha != expected_sha256.lower():
        if part_path.exists():
            part_path.unlink()
        return False, f"SHA256 校验和不匹配（计算值 {computed_sha}）"

    part_path.replace(dest_path)
    return True, ""


def create_data_backup(dest_tar: Path, repo_dir: Path | None = None) -> bool:
    """Backup critical user configuration and state files before upgrading."""
    base = repo_dir or Path(__file__).resolve().parent.parent
    dest_tar.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(dest_tar, "w:gz") as tar:
            for pattern in DATA_BACKUP_ITEMS:
                for path in base.glob(pattern):
                    if path.exists():
                        rel = path.relative_to(base)
                        try:
                            tar.add(str(path), arcname=str(rel))
                        except Exception as e:
                            logger.error("Failed to backup %s: %s", path, e)
                            return False
    except Exception as e:
        logger.error("Failed to create data backup archive %s: %s", dest_tar, e)
        return False
    return dest_tar.is_file() and dest_tar.stat().st_size > 0


class NativeAppCenterClient:
    """HTTP client communicating with fnOS app center daemon over Unix domain socket."""

    def __init__(self, socket_path: str = DAEMON_SOCKET):
        self.socket_path = socket_path

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        if not os.path.exists(self.socket_path):
            raise ConnectionError(f"Daemon socket missing: {self.socket_path}")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(self.socket_path)

        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        req_bytes = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1") + payload

        sock.sendall(req_bytes)

        resp = http.client.HTTPResponse(sock)
        resp.begin()
        data = resp.read()
        sock.close()

        if not data:
            return {}
        return json.loads(data.decode("utf-8"))

    def check_capability(self) -> bool:
        """Probe upgrade route. Empty body triggers code 10030 if route is supported."""
        try:
            res = self._request("POST", "/rpc/v1/update/info", {})
            return res.get("code") == 10030
        except Exception:
            return False

    def stage_fpk(self, fpk_path: str, timeout: float = 180.0) -> dict[str, Any]:
        """Submit FPK to /rpc/v1/download/task and wait for staging completion."""
        res = self._request("POST", "/rpc/v1/download/task", {
            "packageSourceType": "file",
            "path": os.path.abspath(fpk_path),
        })
        task_id = (res.get("data") or {}).get("downloadTaskId")
        if not task_id:
            raise RuntimeError(f"暂存 FPK 失败: {res.get('msg') or res.get('code')}")

        deadline = time.time() + timeout
        while time.time() < deadline:
            status_res = self._request("POST", "/rpc/v1/download/status", {"downloadTaskId": task_id})
            data = status_res.get("data") or {}
            status = data.get("status")
            if status == 2:  # daemonStatusSuccess
                return data
            if status not in (1, None):  # not running
                raise RuntimeError(f"暂存失败（状态 {status}）: {data.get('message')}")
            time.sleep(1.0)
        raise TimeoutError("暂存安装包超时")

    def get_update_info(self, app_name: str, version: str, package_type: str = "fpk") -> int:
        res = self._request("POST", "/rpc/v1/update/info", {
            "appName": app_name,
            "updateVersion": version,
            "packageType": package_type,
            "language": "zh-CN",
        })
        if res.get("code") != 0:
            raise RuntimeError(f"查询升级信息失败: code {res.get('code')} {res.get('msg')}")
        wizard_info = (res.get("data") or {}).get("wizardInfo") or {}
        volume_id = wizard_info.get("installedVolumeID")
        if not isinstance(volume_id, int) or volume_id <= 0:
            raise RuntimeError("无法识别已安装应用存储卷")
        return volume_id

    def submit_update(self, app_name: str, version: str, volume_id: int, package_type: str = "fpk") -> str:
        res = self._request("POST", "/rpc/v1/update/task", {
            "appName": app_name,
            "updateVersion": version,
            "packageType": package_type,
            "systemParameters": {
                "agreedToProtocol": True,
                "installVolumeID": volume_id,
                "dataVolumeId": volume_id,
                "immediateStart": False,
            },
            "customParameters": [],
            "language": "zh-CN",
        })
        if res.get("code") != 0:
            raise RuntimeError(f"提交升级失败: code {res.get('code')} {res.get('msg')}")
        task_id = (res.get("data") or {}).get("taskId")
        if not task_id:
            raise RuntimeError("应用中心未返回升级任务 ID")
        return str(task_id)

    def wait_task(self, task_id: str, timeout: float = 1800.0) -> tuple[int, str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self._request("POST", "/rpc/v1/common/status", {"taskId": task_id})
            data = res.get("data") or {}
            status = data.get("status")
            if status == 2:  # Success
                return 2, "升级任务执行成功"
            if status == 5:  # Unknown / reaped
                return 5, "应用中心不再持有该任务，需核对已装状态"
            if status not in (1, None):
                msg = data.get("message") or data.get("outputText") or f"状态 {status}"
                return status, str(msg)
            time.sleep(2.0)
        return 0, "等待任务完成超时"

    def verify_installed(self, app_name: str, expected_version: str) -> tuple[bool, str]:
        res = self._request("GET", "/rpc/v1/app/installed")
        data = res.get("data") or {}
        apps = data.get("list") if isinstance(data, dict) else []
        for app in apps:
            if isinstance(app, dict) and app.get("appName") == app_name:
                cur_ver = str(app.get("version") or "")
                if cur_ver == expected_version:
                    return True, cur_ver
                return False, f"版本不一致（当前 {cur_ver}，期望 {expected_version}）"
        return False, f"已安装列表中未找到 {app_name}"


def execute_job(job_dir: Path) -> int:
    """Core worker logic running the download, backup, and upgrade."""
    job = _read_job_state(job_dir)
    if not job:
        return 1

    snap = get_target_snapshot(job.target_id)
    if not snap:
        target_file = job_dir / "target.json"
        if target_file.is_file():
            try:
                snap_dict = json.loads(target_file.read_text(encoding="utf-8"))
                snap = TargetSnapshot(**snap_dict)
            except Exception:
                pass
    if not snap:
        job.status = "failed"
        job.message = "升级目标快照已失效"
        _save_job_state(job_dir, job)
        return 1

    job.status = "running"
    job.phase = "downloading"
    _save_job_state(job_dir, job)

    dest_fpk = job_dir / snap.asset_name
    download_ok = False
    last_err = ""

    def update_progress(cur: int, tot: int) -> None:
        job.downloaded_bytes = cur
        job.total_bytes = tot
        _save_job_state(job_dir, job)

    # Try candidates in order: Gitee first, then fallback to GitHub
    for cand in snap.candidates:
        job.source = cand.get("source", "")
        _save_job_state(job_dir, job)
        ok, err = _download_and_verify(cand, snap, dest_fpk, progress_cb=update_progress)
        if ok:
            download_ok = True
            break
        last_err = err

    if not download_ok:
        job.status = "failed"
        job.phase = "downloading"
        job.error_code = "DOWNLOAD_FAILED"
        job.message = FALLBACK_FAIL_MESSAGE
        _save_job_state(job_dir, job)
        return 2

    # Step: backing up
    job.phase = "backing_up"
    _save_job_state(job_dir, job)
    backup_tar = job_dir / "backup.tar.gz"
    if not create_data_backup(backup_tar):
        job.status = "failed"
        job.message = "升级前数据保护备份失败，已中止升级"
        _save_job_state(job_dir, job)
        return 3

    if not os.path.exists(DAEMON_SOCKET):
        job.status = "failed"
        job.phase = "installing"
        job.error_code = "DAEMON_UNAVAILABLE"
        job.message = "FPK 已下载并校验，但应用中心不可用，未安装更新"
        _save_job_state(job_dir, job)
        return 5

    client = NativeAppCenterClient(DAEMON_SOCKET)
    try:
        job.phase = "staging"
        _save_job_state(job_dir, job)
        staged = client.stage_fpk(str(dest_fpk))
        staged_app = staged.get("appName")
        staged_ver = staged.get("version")
        if staged_app != "fnmusic-ext":
            raise RuntimeError(f"安装包应用名不匹配: {staged_app}")
        if staged_ver != snap.install_version:
            raise RuntimeError(f"安装包版本不匹配: {staged_ver}（期望 {snap.install_version}）")

        job.phase = "installing"
        _save_job_state(job_dir, job)
        volume_id = client.get_update_info("fnmusic-ext", staged_ver)
        task_id = client.submit_update("fnmusic-ext", staged_ver, volume_id)
        job.daemon_task_id = task_id
        _save_job_state(job_dir, job)

        status_code, msg = client.wait_task(task_id)
        if status_code not in (2, 5):
            raise RuntimeError(f"原生升级执行失败: {msg}")

        # Verify final installation state
        verified, ver_msg = client.verify_installed("fnmusic-ext", snap.install_version)
        if not verified and status_code == 5:
            job.status = "unknown"
            job.message = f"升级结果待确认：应用中心任务已释放，但验证未通过（{ver_msg}）"
            _save_job_state(job_dir, job)
            return 4
        elif not verified:
            raise RuntimeError(f"升级完成验证失败: {ver_msg}")

        job.status = "completed"
        job.phase = "done"
        job.message = f"升级成功，已安装版本 {snap.install_version}"
        _save_job_state(job_dir, job)

        # Cleanup large downloaded FPK once verified
        if dest_fpk.is_file():
            try:
                dest_fpk.unlink()
            except OSError:
                pass
        return 0
    except Exception as exc:
        job.status = "failed"
        job.message = f"升级流程异常: {exc}"
        _save_job_state(job_dir, job)
        return 5


def start_update_job(target_id: str) -> tuple[int, dict[str, Any]]:
    """Create and start a new background update job from a valid target snapshot."""
    with _UPDATE_LOCK:
        return _start_update_job_locked(target_id)


def _start_update_job_locked(target_id: str) -> tuple[int, dict[str, Any]]:
    global _ACTIVE_JOB
    snap = get_target_snapshot(target_id)
    if not snap:
        return 409, {"ok": False, "error": "目标版本快照不存在或已过期，请重新检查更新"}

    active_job = get_active_job()
    if active_job and active_job.status in ("queued", "running"):
        if active_job.target_id == target_id:
            return 202, {"ok": True, "job": active_job.to_dict()}
        return 409, {"ok": False, "error": "已有其他更新任务正在执行中"}

    job_id = secrets.token_hex(16)
    now = time.time()
    job = UpdateJob(
        id=job_id,
        target_id=target_id,
        tag=snap.tag,
        install_version=snap.install_version,
        architecture=snap.architecture,
        status="queued",
        phase="downloading",
        source="",
        downloaded_bytes=0,
        total_bytes=snap.size or 0,
        daemon_task_id="",
        error_code="",
        message="任务已排队",
        created_at=now,
        updated_at=now,
    )

    base_dir = get_job_base_dir()
    job_dir = base_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    (job_dir / "target.json").write_text(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    _save_job_state(job_dir, job)
    _ACTIVE_JOB = job

    # Launch worker
    worker_cmd = [
        sys.executable,
        "-c",
        "import sys; from proxy.self_update import execute_job; from pathlib import Path; sys.exit(execute_job(Path(sys.argv[1])))",
        str(job_dir),
    ]

    # On Linux with systemd, use systemd-run for independence from main unit lifecycle
    if shutil.which("systemd-run") and os.name != "nt" and os.getuid() == 0:
        unit_name = f"fnmusic-ext-update-{job_id[:8]}"
        work_dir = str(Path(__file__).resolve().parent.parent)
        cmd = [
            "systemd-run", f"--unit={unit_name}", "--description=fnmusic-ext-update-worker",
            f"--working-directory={work_dir}", "--",
        ] + worker_cmd
        try:
            started = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if started.returncode == 0:
                return 202, {"ok": True, "job": job.to_dict()}
        except subprocess.TimeoutExpired:
            return 503, {
                "ok": False,
                "error": "启动升级工作进程超时；为避免重复安装，请先核对任务和应用中心状态",
            }
        except OSError:
            pass

    # Detached subprocess fallback
    try:
        subprocess.Popen(
            worker_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=(os.name != "nt"),
            cwd=str(Path(__file__).resolve().parent.parent),
            start_new_session=(os.name == "posix"),
        )
    except Exception as exc:
        job.status = "failed"
        job.message = f"启动升级工作进程失败: {exc}"
        _save_job_state(job_dir, job)
        return 500, {"ok": False, "error": job.message}

    return 202, {"ok": True, "job": job.to_dict()}


def get_job(job_id: str) -> UpdateJob | None:
    global _ACTIVE_JOB
    if _ACTIVE_JOB and _ACTIVE_JOB.id == job_id:
        base_dir = get_job_base_dir()
        disk_state = _read_job_state(base_dir / job_id)
        if disk_state:
            _ACTIVE_JOB = disk_state
        return _ACTIVE_JOB

    base_dir = get_job_base_dir()
    job_dir = base_dir / job_id
    return _read_job_state(job_dir)


def get_active_job() -> UpdateJob | None:
    global _ACTIVE_JOB
    if _ACTIVE_JOB:
        base_dir = get_job_base_dir()
        disk_state = _read_job_state(base_dir / _ACTIVE_JOB.id)
        if disk_state:
            _ACTIVE_JOB = disk_state
        return _ACTIVE_JOB

    # Check disk for active or most recent job
    base_dir = get_job_base_dir()
    candidates = sorted(base_dir.glob("*/job.json"), key=os.path.getmtime, reverse=True)
    for cand in candidates[:5]:
        job = _read_job_state(cand.parent)
        if job:
            if job.status in ("queued", "running"):
                _ACTIVE_JOB = job
                return job
    return None


def get_most_recent_job() -> UpdateJob | None:
    job = get_active_job()
    if job:
        return job
    base_dir = get_job_base_dir()
    candidates = sorted(base_dir.glob("*/job.json"), key=os.path.getmtime, reverse=True)
    for cand in candidates[:5]:
        j = _read_job_state(cand.parent)
        if j:
            return j
    return None
