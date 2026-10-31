#!/bin/sh
# fnmusic-sources 容器入口：supervisor 起来后按 /repo/.env 只拉起所选音源（+WebUI）。
# 全部程序 autostart=false（supervisord.conf），实现按需加载：
# 未启用的音源进程不驻留内存；容器重启后本脚本按 .env 自愈恢复同样的进程集。
set -eu

SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

log() { echo "[entrypoint] $*"; }

export_source_env

# 确保 lxserver 数据目录与初始配置就绪
init_lxserver_config() {
    _lx_data="${DATA_PATH:-/data/lxserver}"
    mkdir -p "${_lx_data}/backups" "${_lx_data}/users/source/_open"
    _cfg_file="${_lx_data}/config.js"
    if [ ! -f "${_cfg_file}" ] || (grep -q "frontend:" "${_cfg_file}" 2>/dev/null && ! grep -q "frontend.password" "${_cfg_file}" 2>/dev/null); then
        # 默认密码从环境变量获取，或缺省为 123456
        _admin_pw="${LXSERVER_ADMIN_PASSWORD:-123456}"
        _escaped_pw="$(printf '%s' "${_admin_pw}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
        cat <<EOF > "${_cfg_file}"
module.exports = {
  serverName: "fnmusic-lxserver",
  bindIP: "127.0.0.1",
  port: 8005,
  "frontend.password": "${_escaped_pw}",
  "player.enableAuth": false,
  "subsonic.enable": false,
  "webdav.enable": false,
  "system.allowUnsafeVM": false
};
EOF
        log "已初始化/对齐 lxserver 配置: ${_cfg_file}"
    fi

    # 历史数据无损迁移：从旧 /data/lxmusic 迁移用户自定义源脚本到 lxserver
    _old_lx_data="${LX_DATA_DIR:-/data/lxmusic}"
    if [ -d "${_old_lx_data}/uploads" ]; then
        for _f in "${_old_lx_data}/uploads"/*.js; do
            if [ -f "${_f}" ]; then
                _base="$(basename "${_f}")"
                if [ ! -f "${_lx_data}/users/source/_open/${_base}" ]; then
                    cp -a "${_f}" "${_lx_data}/users/source/_open/"
                    log "已从旧版本迁移自定义源脚本: ${_base}"
                fi
            fi
        done
    fi
    if [ -f "${_old_lx_data}/source.js" ] && [ ! -f "${_lx_data}/users/source/_open/migrated_source.js" ]; then
        if [ ! "$(ls -A "${_lx_data}/users/source/_open" 2>/dev/null)" ]; then
            cp -a "${_old_lx_data}/source.js" "${_lx_data}/users/source/_open/migrated_source.js"
            log "已迁移旧版本激活的自定义源 source.js"
        fi
    fi
}

init_lxserver_config

# supervisord 要求 logfile 目录必须已存在，否则解析配置即失败、全部音源起不来
# （CI 离线容器实证）。日志根与 fnlog 解析优先级同源：容器 /repo/logs（Dockerfile
# 已导出 FNMUSIC_LOG_DIR），native 由 systemd unit 导出仓库 logs 目录。
mkdir -p "${FNMUSIC_LOG_DIR:-/repo/logs}"

supervisord -c "$SUP_CONF" &
SUP_PID=$!

# 等 supervisor 控制套接字就绪（最多 10s）。
_i=0
until supervisorctl -c "$SUP_CONF" pid >/dev/null 2>&1; do
    _i=$((_i + 1))
    if [ "$_i" -ge 100 ]; then
        log "supervisor 套接字未就绪，继续等待（进程状态将由 healthcheck 反映）"
        break
    fi
    sleep 0.1
done

start_prog() {
    if supervisorctl -c "$SUP_CONF" start "$1" >/dev/null 2>&1; then
        log "started $1"
    else
        log "WARN: start $1 失败（supervisorctl status 查看原因）"
    fi
}

# 有界就绪等待：慢初始化（脚本首次加载、依赖下载）只是耗时长，不是故障，
# 因此这里只等、不重启进程、不停进程、不动用户音源；等不到就把结论写进
# 容器日志供导出。默认 0 秒（关闭）：容器按需启动要快速返回，就绪状态由
# healthcheck 的进程层 + HTTP 层两层检查持续反映，不需要入口阻塞。
# 需要等链路就绪的场景（例如 NAS 慢盘首次初始化）可显式设置该环境变量。
LXMUSIC_HEALTH_URL="${LXMUSIC_HEALTH_URL:-http://127.0.0.1:8003/healthz}"
LX_READY_WAIT="${LX_READY_WAIT:-0}"
wait_ready() {
    _prog="$1"
    _url="$2"
    _remaining="$LX_READY_WAIT"
    case "$_remaining" in
        ''|*[!0-9]*) log "WARN: LX_READY_WAIT 非数字（$_remaining），跳过就绪等待"; return 0 ;;
    esac
    [ "$_remaining" -gt 0 ] || return 0
    _end=$(( $(date +%s) + _remaining ))
    while [ "$(date +%s)" -lt "$_end" ]; do
        _code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$_url" 2>/dev/null)"
        case "$_code" in
            2??) log "$_prog 已就绪（HTTP $_code）"; return 0 ;;
        esac
        sleep 2
    done
    log "WARN: $_prog 在 ${LX_READY_WAIT}s 内未通过 HTTP 就绪检查"
    log "WARN: 进程保持不变（不重启、不停用），用户音源不清空；状态由 healthcheck 反映"
    return 1
}

# 三选一音源：只启动 .env 选中的那个（多开视为异常，全部忽略只取第一个命中）
if [ "$(env_flag FNMUSIC_MUSICDL_ENABLED)" = "true" ]; then
    start_prog musicdl
elif [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" = "true" ]; then
    start_prog musicbox
elif [ "$(env_flag FNMUSIC_LX_ENABLED)" = "true" ]; then
    start_prog lxserver
    start_prog lxmusic
    # 有界等待整条链路就绪；失败只记录，不停进程也不清空用户音源。
    wait_ready lxmusic "$LXMUSIC_HEALTH_URL" || true
else
    log "未配置任何音源（FNMUSIC_MUSICDL_ENABLED/FNMUSIC_NETEASE_ENABLED/FNMUSIC_LX_ENABLED 均未启用）"
fi

# 网易云歌单常驻（issue #38）：当前音源不是 musicbox 但勾选常驻，且有网易
# 账号歌单（盘上有 cookie，登录启发）或网易公共歌单选择时，拉起 musicbox。
# 权威校验由 webui 开机对账完成。原生形态 entrypoint 无 XDG 环境变量，可能
# 找不到 cookie 而跳过——由 webui 开机对账兜底拉起。
musicbox_cookie_present() {
    for _f in "${XDG_DATA_HOME:-/nonexistent}/netease-musicbox/cookie.txt" \
              "${HOME:-/root}/.netease-musicbox/cookie.txt" \
              "${XDG_CONFIG_HOME:-/nonexistent}/netease-musicbox/cookie.txt"; do
        if [ -s "$_f" ]; then
            return 0
        fi
    done
    return 1
}

public_musicbox_selected() {
    [ "$(env_flag FNMUSIC_PUBLIC_PLAYLISTS_ENABLED)" = "true" ] || return 1
    python3 -c 'import json, os, sys
sys.path[:0] = [sys.argv[2], "/srv"]
from proxy.env_merge import parse_env_file
try:
    values = dict(os.environ)
    if os.path.isfile(sys.argv[1]):
        values.update(dict(parse_env_file(sys.argv[1], strict=True)[0]))
    raw = values.get("FNMUSIC_PUBLIC_PLAYLISTS_SELECTION", "")
    per_user = values.get("FNMUSIC_PUBLIC_PLAYLISTS_BY_USER", "").strip()
    if per_user:
        mapping = json.loads(per_user)
        rows = [item for selection in mapping.values() if isinstance(selection, list) for item in selection] if isinstance(mapping, dict) else []
    else:
        rows = json.loads(raw) if raw.strip() else [{"provider": "musicbox", "id": "3778678"}]
    needed = isinstance(rows, list) and any(isinstance(x, dict) and x.get("provider") == "musicbox" and x.get("id") for x in rows)
except (ValueError, TypeError, OSError):
    needed = False
sys.exit(0 if needed else 1)' "$ENV_FILE" "$(dirname "$0")/.."
}

if [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" != "true" ] \
   && [ "$(env_flag FNMUSIC_NETEASE_PLAYLIST_RESIDENT)" = "true" ] \
   && { public_musicbox_selected \
        || { [ "$(env_flag FNMUSIC_NETEASE_MY_PLAYLISTS)" = "true" ] && musicbox_cookie_present; }; }; then
    start_prog musicbox
fi

# WebUI 默认随容器启动（--no-webui 安装会写 FNMUSIC_WEBUI_ENABLED=false）
if [ "$(env_flag FNMUSIC_WEBUI_ENABLED)" = "true" ]; then
    start_prog webui
fi

# 信号转发：docker stop → supervisorctl shutdown → 各程序优雅退出
trap 'supervisorctl -c "$SUP_CONF" shutdown >/dev/null 2>&1' TERM INT
log "fnmusic-sources ready (pid=$SUP_PID)"
wait "$SUP_PID"
