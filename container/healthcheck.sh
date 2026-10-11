#!/bin/sh
# 容器健康检查：按 .env 应启的程序在 supervisor 里均为 RUNNING 才算健康。
set -eu
SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

running() {
    supervisorctl -c "$SUP_CONF" status "$1" 2>/dev/null | grep -q "RUNNING"
}

fail=0
checked=0

check() {
    prog="$1"; key="$2"; default="${3:-false}"
    if [ "$(env_flag "$key" "$default")" = "true" ]; then
        checked=$((checked + 1))
        if ! running "$prog"; then
            echo "[healthcheck] $prog 应运行但状态异常" >&2
            fail=1
        fi
    fi
}

check musicdl FNMUSIC_MUSICDL_ENABLED
check musicbox FNMUSIC_NETEASE_ENABLED
# 网易云歌单常驻（issue #38）：musicbox 非当前音源但常驻开启时应常驻运行。
# 只捕捉崩溃循环（FATAL/BACKOFF）；webui 对账的主动停止（如未登录回收）属
# 正常态，不算失败——避免 cookie 失效期间容器长期 unhealthy。
if [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" != "true" ] \
   && [ "$(env_flag FNMUSIC_NETEASE_PLAYLIST_RESIDENT)" = "true" ] \
   && [ "$(env_flag FNMUSIC_NETEASE_MY_PLAYLISTS)" = "true" ]; then
    checked=$((checked + 1))
    _mb_state="$(supervisorctl -c "$SUP_CONF" status musicbox 2>/dev/null || true)"
    case "$_mb_state" in
        *FATAL*|*BACKOFF*)
            echo "[healthcheck] musicbox 常驻进程崩溃循环（FATAL/BACKOFF）" >&2
            fail=1
            ;;
    esac
fi
if [ "$(env_flag FNMUSIC_LX_ENABLED)" = "true" ]; then
    checked=$((checked + 1))
    if ! running lxserver; then
        echo "[healthcheck] lxserver 应运行但状态异常" >&2
        fail=1
    fi
    if ! running lxmusic; then
        echo "[healthcheck] lxmusic 应运行但状态异常" >&2
        fail=1
    fi
fi
check webui FNMUSIC_WEBUI_ENABLED

if [ "$checked" -eq 0 ]; then
    echo "[healthcheck] 未配置任何音源" >&2
    exit 1
fi
exit "$fail"
