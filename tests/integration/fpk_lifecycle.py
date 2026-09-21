#!/usr/bin/env python3
"""fnOS 应用中心 fpk 安装/卸载自动测试（实机专用，需 root）。

基于官方 appcenter-cli 对 build.sh 产出的 .fpk 做完整生命周期验证：
  安装（向导 env 注入）→ 健康断言（WebUI 8774 + socket 接管 /_ext/healthz）
  → stop（官方直连还原断言）→ start（恢复断言）
  → 升级链（直调 upgrade_init/upgrade_callback，伪造 TRIM_PKGVAR：
     备份→模拟覆盖→恢复重装，CLI 不支持升级故绕行）
  → 卸载 → 清理与数据备份断言。

实机行为备注（fnOS 1.2.0604 / appcenter-cli 1.0.1 实测）:
  - install-fpk 不做版本升级（已安装时直接跳过），升级需在应用中心 Web UI 操作；
  - 安装完成后应用会自动启动，立刻 stop 会撞上启动任务报 10500，
    故安装后先等待稳态（status=running 且 unit active）再发 stop；
  - CLI 对 stop/start/uninstall 可能返回 rc=0 但输出 [Error]，必须同时检查输出。

本测试会接管官方音乐 socket 并操作 systemd/docker，必须先释放既有部署：
  - 默认：检测到已有部署/已装应用时中止并列出处理命令
  - --auto-restore：自动对既有部署执行 restore.sh 后继续
测试结束后原部署不会自动恢复，按结尾提示执行 ./extend.sh 重新启用。

用法（在飞牛 fnOS 设备上）:
  sudo python3 tests/integration/fpk_lifecycle.py [--fpk PATH] [--sources musicdl]
        [--auto-restore] [--skip-uninstall] [--timeout 600]
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APPNAME = "fnmusic-ext"
UNIT = "fnmusic-ext.service"
SOCKET = "/var/run/trim_music.socket"
WEBUI = "http://127.0.0.1:8774/healthz"

RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def run(cmd, timeout=None, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def cli(app_args, timeout=None):
    """appcenter-cli 调用：rc=0 且输出无 [Error] 才算成功。"""
    out = run(["appcenter-cli", *app_args], timeout=timeout)
    text = (out.stdout + out.stderr).replace("\r", "\n")
    ok = out.returncode == 0 and "[Error]" not in text
    return ok, text


def record(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def http_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def socket_ext_healthy() -> bool:
    """经官方 socket 路径访问 /_ext/livez 并校验响应体。

    只有代理接管时才有该端点；官方直连时 trim-music 的 SPA 兜底路由同样
    返回 HTTP 200，因此必须检查响应体包含 fnmusic-ext 标识，不能只看状态码。
    """
    import json as jsonmod
    import socket as sockmod

    try:
        s = sockmod.socket(sockmod.AF_UNIX, sockmod.SOCK_STREAM)
        s.settimeout(4.0)
        s.connect(SOCKET)
        s.sendall(b"GET /_ext/livez HTTP/1.0\r\nHost: localhost\r\n\r\n")
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        if b" 200 " not in head.split(b"\r\n", 1)[0]:
            return False
        payload = jsonmod.loads(body.decode(errors="replace"))
        return payload.get("service") == "fnmusic-ext"
    except (OSError, ValueError):
        return False


def systemd_active() -> bool:
    return run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0


def wait_for(check, timeout: float, interval: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def preflight(args) -> None:
    if os.geteuid() != 0:
        sys.exit("需要 root：请用 sudo 运行（appcenter-cli 仅 root 可执行）")
    for tool in ("appcenter-cli", "docker", "systemctl"):
        if not shutil.which(tool):
            sys.exit(f"缺少 {tool}（本测试只能在飞牛 fnOS 设备上运行）")
    if not Path(SOCKET).exists():
        sys.exit("未检测到官方音乐 socket，请先安装并启动官方\"飞牛音乐\"")

    # 既有部署（git clone 方式）会与 fpk 安装争用 unit/容器名/部署登记
    deployed_dir = ""
    if Path(f"/etc/systemd/system/{UNIT}").exists():
        out = run(["systemctl", "show", UNIT, "-p", "WorkingDirectory", "--value"])
        deployed_dir = out.stdout.strip()
    if not deployed_dir:
        out = run(["docker", "container", "inspect", "fnmusic-sources",
                   "--format", "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}"])
        if out.returncode == 0:
            deployed_dir = out.stdout.strip()
    if deployed_dir and Path(deployed_dir, "restore.sh").is_file():
        if not args.auto_restore:
            sys.exit(
                f"检测到既有部署: {deployed_dir}\n"
                f"fpk 安装会与其冲突。请先执行: cd {deployed_dir} && sudo ./restore.sh\n"
                "或使用 --auto-restore 由本测试自动还原后继续。"
            )
        print(f"[preflight] 自动还原既有部署: {deployed_dir}", flush=True)
        out = run(["bash", str(Path(deployed_dir) / "restore.sh")], timeout=300)
        if out.returncode != 0:
            sys.exit(f"restore.sh 失败:\n{out.stdout}\n{out.stderr}")
    elif deployed_dir:
        sys.exit(f"检测到不明部署目录 {deployed_dir}（无 restore.sh），请手动处理后重试")

    # 旧的 fpk 安装残留
    if Path(f"/var/apps/{APPNAME}").exists():
        if not args.auto_restore:
            sys.exit(f"/var/apps/{APPNAME} 已存在，请先: appcenter-cli uninstall {APPNAME}\n或使用 --auto-restore")
        print(f"[preflight] 卸载旧的 {APPNAME} 安装", flush=True)
        ok, text = cli(["uninstall", APPNAME], timeout=args.timeout)
        if not ok:
            sys.exit(f"卸载旧安装失败:\n{text}")


def build_fpk(args) -> Path:
    print("[build] packaging/fpk/build.sh", flush=True)
    out = run(["bash", str(REPO_ROOT / "packaging" / "fpk" / "build.sh")], timeout=300)
    if out.returncode != 0:
        sys.exit(f"build.sh 失败:\n{out.stdout}\n{out.stderr}")
    version = (REPO_ROOT / "VERSION").read_text().strip()
    fpk = REPO_ROOT / "dist" / f"fnmusic-ext-{version}.fpk"
    if not fpk.is_file():
        sys.exit(f"未找到打包产物 {fpk}")
    print(f"[build] {fpk} ({fpk.stat().st_size} bytes)", flush=True)
    return fpk


def install_fpk(args, fpk: Path) -> None:
    print(f"[install] appcenter-cli install-fpk（sources={args.sources}）", flush=True)
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as envf:
        envf.write(f"wizard_sources={args.sources}\nwizard_extend=true\n")
        env_path = envf.name
    try:
        ok, text = cli(["install-fpk", str(fpk), "--env", env_path], timeout=args.timeout)
    finally:
        os.unlink(env_path)
    if not ok:
        sys.exit(f"install-fpk 失败:\n{text}")
    log = Path(f"/var/apps/{APPNAME}/var/fnmusic-app.log")
    print("[install] 安装日志尾部:", flush=True)
    for line in (log.read_text(errors="replace").splitlines() or ["(空)"])[-15:]:
        print(f"    {line}")


def wait_app_steady(args) -> None:
    """安装后 appcenter 会自动启动应用；立即 stop 会与启动任务冲突（10500）。"""
    print("[settle] 等待应用进入稳态（status=running 且 unit active）...", flush=True)
    steady = wait_for(
        lambda: run(["appcenter-cli", "status", APPNAME]).stdout.strip().endswith("running")
        and systemd_active(),
        timeout=180,
    )
    record(steady, "应用自动启动完成（appcenter status=running）")
    time.sleep(10)  # 启动任务收尾缓冲


def assert_installed(args) -> None:
    print("[assert] 安装后健康检查", flush=True)
    record(Path(f"/var/apps/{APPNAME}").is_dir(), "/var/apps/fnmusic-ext 已创建")
    listing = run(["appcenter-cli", "list"]).stdout
    record(APPNAME in listing, "appcenter-cli list 出现 fnmusic-ext")
    record(http_ok(WEBUI), "WebUI :8774 /healthz 返回 200")
    record(socket_ext_healthy(), "socket 接管生效（/_ext/healthz 经官方 socket 返回 200）")


def assert_stop(args) -> None:
    print("[stop] appcenter-cli stop → 官方直连还原", flush=True)
    ok, text = cli(["stop", APPNAME], timeout=args.timeout)
    record(ok, "stop 命令成功", next((l for l in text.splitlines() if l.strip() and "stopping" not in l), "")[:120])
    record(wait_for(lambda: not systemd_active(), 60), "fnmusic-ext.service 已停止")
    record(not http_ok(WEBUI), "WebUI 已随容器停止")
    # 停止后官方 socket 必须仍存在且由官方后端应答（/_ext/* 不再存在）
    record(Path(SOCKET).exists(), "官方 trim_music.socket 仍存在")
    record(not socket_ext_healthy(), "/_ext/healthz 已下线（还原官方直连）")


def assert_start(args) -> None:
    print("[start] appcenter-cli start → 恢复扩展", flush=True)
    ok, text = cli(["start", APPNAME], timeout=args.timeout)
    record(ok, "start 命令成功", "Launching complete" if "Launching complete" in text else text.replace("\r", " ").strip()[:120])
    record(wait_for(lambda: socket_ext_healthy() and systemd_active(), 300),
           "socket 接管恢复（/_ext/healthz 200 + unit active）")
    record(wait_for(lambda: http_ok(WEBUI), 120), "WebUI 恢复 200")


def run_upgrade_hooks(args) -> None:
    """直调升级钩子：appcenter-cli 不支持升级（升级只在应用中心 Web UI 触发），
    但 fnOS 升级时对 target/ 整体换新后只会执行 upgrade_init/upgrade_callback，
    这条链可以在实机直接驱动验证。

    伪造 TRIM_PKGVAR 指向临时目录，完整走一遍：
    打标 → upgrade_init（停服+备份）→ 模拟 fnOS 覆盖（清空 repo 用户数据）
    → upgrade_callback（恢复备份 + 按恢复的 .env 重装）→ 健康断言。
    """
    print("[upgrade] 直调 upgrade_init / upgrade_callback（TRIM_PKGVAR=临时目录）", flush=True)
    cmd_dir = Path(f"/var/apps/{APPNAME}/target/cmd")
    upgrade_init = cmd_dir / "upgrade_init"
    upgrade_callback = cmd_dir / "upgrade_callback"
    repo = Path(f"/var/apps/{APPNAME}/target/repo")
    if not (upgrade_init.is_file() and upgrade_callback.is_file() and repo.joinpath("install.sh").is_file()):
        record(False, "升级钩子就位", f"{cmd_dir} 或 {repo} 内容不完整")
        return

    pkgvar = Path(tempfile.mkdtemp(prefix="fnmusic-upgrade-"))
    env = {**os.environ, "TRIM_PKGVAR": str(pkgvar)}
    backup = pkgvar / "upgrade-backup" / "data.tar.gz"

    marker = f"CUSTOM_UPGRADE_MARKER='{int(time.time())}'"
    env_file = repo / ".env"
    base_env = env_file.read_text()
    env_file.write_text(base_env + f"\n{marker}\n")
    try:
        out = run(["bash", str(upgrade_init)], timeout=120, env=env)
        record(out.returncode == 0, "upgrade_init 成功", (out.stderr or "").strip()[:120])
        record(not systemd_active(), "upgrade_init 已停服务")
        record(backup.is_file(), "备份 data.tar.gz 已生成")
        if backup.is_file():
            members = run(["tar", "-tzf", str(backup)]).stdout.split()
            record(".env" in members, "备份包含 .env")
            record(marker in run(["tar", "-xzOf", str(backup), ".env"]).stdout,
                   "备份内容是打标后的 .env（备份先于覆盖）")

        # 模拟 fnOS 升级覆盖 target/：新包不含用户数据，repo 内数据项消失
        for item in (".env", "online_favorites", "online_favorites.json", "play_history"):
            p = repo / item
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        record(not env_file.exists(), "已模拟升级覆盖（repo 用户数据清空）")

        out = run(["bash", str(upgrade_callback)], timeout=args.timeout, env=env)
        record(out.returncode == 0, "upgrade_callback 成功", (out.stderr or "").strip()[:200])
        record(env_file.is_file() and marker in env_file.read_text(), "用户数据已恢复（.env 标记回归）")
        record(not pkgvar.joinpath("upgrade-backup").exists(), "升级备份已按设计清理")
        record(wait_for(systemd_active, 180), "服务随重装恢复 active")
        record(wait_for(socket_ext_healthy, 300), "socket 接管恢复（/_ext/healthz 200）")
        record(wait_for(lambda: http_ok(WEBUI), 120), "WebUI 恢复 200")
    finally:
        # 保底去标记：升级链中途失败时不把测试标记留在真实 .env 里。
        # 仅在数据确认恢复后才清理临时目录——失败时该备份可能是用户数据唯一副本，
        # 与 upgrade_callback 自身"失败保留备份"的契约一致。
        restored = env_file.is_file() and marker in env_file.read_text()
        if restored:
            env_file.write_text(base_env)
            shutil.rmtree(pkgvar, ignore_errors=True)
        else:
            print(f"    升级链未完成：用户数据备份保留在 {backup}", flush=True)


def uninstall(args) -> None:
    print(f"[uninstall] appcenter-cli uninstall {APPNAME}", flush=True)
    # 实际存储卷从 target 符号链接解析（如 /vol2/@appcenter/fnmusic-ext → /vol2/）
    vol_root = "/vol1/"
    try:
        link = os.readlink(f"/var/apps/{APPNAME}/target")
        if "/@appcenter/" in link:
            vol_root = link.split("/@appcenter/")[0] + "/"
    except OSError:
        pass
    ok, text = cli(["uninstall", APPNAME], timeout=args.timeout)
    record(ok, "uninstall 命令成功", next((l for l in text.splitlines() if l.strip() and "uninstalling" not in l), "")[:120])
    record(not Path(f"/var/apps/{APPNAME}").exists(), "/var/apps/fnmusic-ext 已移除")
    record(not Path(f"/etc/systemd/system/{UNIT}").exists(), "systemd unit 已移除")
    record(run(["docker", "container", "inspect", "fnmusic-sources"]).returncode != 0,
           "fnmusic-sources 容器已移除")
    record(Path(SOCKET).exists() and not socket_ext_healthy(),
           "官方音乐恢复直连（socket 存在且 /_ext 已下线）")
    # 卸载钩子应把用户数据备份到存储卷根目录
    backups = sorted(glob.glob(f"{vol_root}fnmusic-ext-backup-*.tar.gz"))
    if record(bool(backups), "卸载前数据备份已生成（vol 根目录 fnmusic-ext-backup-*.tar.gz）",
              backups[-1] if backups else "未找到备份文件"):
        for path in backups:
            os.unlink(path)
        print(f"    已清理测试备份 x{len(backups)}", flush=True)


def summary() -> int:
    print("\n===== 结果 =====", flush=True)
    failed = 0
    for status, name, detail in RESULTS:
        print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))
        failed += status == "FAIL"
    print(f"\n通过 {len(RESULTS) - failed}/{len(RESULTS)}")
    if failed:
        print(f"失败 {failed} 项")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="fpk 安装/卸载生命周期自动测试（实机）")
    parser.add_argument("--fpk", help="使用指定的 .fpk（默认现场打包）")
    parser.add_argument("--sources", default="musicdl", choices=["musicdl", "musicbox", "lxmusic"],
                        help="安装向导注入的初始音源（默认 musicdl）")
    parser.add_argument("--auto-restore", action="store_true",
                        help="自动还原既有部署/卸载旧安装")
    parser.add_argument("--skip-uninstall", action="store_true",
                        help="测试结束后保留安装（跳过卸载断言）")
    parser.add_argument("--timeout", type=int, default=600, help="单条 CLI 命令超时（秒）")
    args = parser.parse_args()

    preflight(args)
    fpk = Path(args.fpk) if args.fpk else build_fpk(args)
    install_fpk(args, fpk)
    wait_app_steady(args)
    assert_installed(args)
    assert_stop(args)
    assert_start(args)
    run_upgrade_hooks(args)
    if not args.skip_uninstall:
        uninstall(args)
        print("\n提示：如需恢复原有 git clone 部署，请在原部署目录执行 sudo ./extend.sh")
    rc = summary()
    if rc == 0 and args.skip_uninstall:
        print("\n注意：--skip-uninstall 生效，fnmusic-ext 仍保持安装并运行。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
