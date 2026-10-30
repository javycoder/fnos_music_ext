#!/bin/sh
# 容器健康检查：按 .env 应启的程序，进程层与 HTTP 层都要通过才算健康。
#
# 两层区分的原因（issue #108 现场实证）：supervisor 报告两个进程 RUNNING，
# 但 lxmusic 查询 lxserver 持续 TimeoutError。只看进程状态的健康检查会把
# 「进程在、服务不响应」一直判成健康，把真实故障藏起来，也不留任何日志。
# 现在进程层结论来自 supervisor，HTTP 层结论来自各服务自报端点，
# 两者分别出结论，避免互相掩盖。
set -eu
SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

# 单个服务的 HTTP 探活上限；健康检查整体还要给 Docker 的 --timeout=10s 让路，
# 因此取 3s：慢初始化不会拖死健康检查，也不至于把瞬时抖动判成故障。
HEALTH_HTTP_TIMEOUT="${HEALTH_HTTP_TIMEOUT:-3}"
# 容器形态的固定内部端口；native 形态的服务借用 ENTRY 时由环境覆盖。
LX_MUSIC_HEALTH_URL="${LX_MUSIC_HEALTH_URL:-http://127.0.0.1:8003/healthz}"
LX_SERVER_HEALTH_URL="${LX_SERVER_HEALTH_URL:-http://127.0.0.1:8005/api/status}"

running() {
    supervisorctl -c "$SUP_CONF" status "$1" 2>/dev/null | grep -q "RUNNING"
}

# HTTP 就绪探活，输出格式 "kind=detail"：超时、连接失败、非 2xx 分开，
# 排障时能直接看出是「进程活着但不响应」还是「响应不符合契约」。
http_ready() {
    label="$1"
    url="$2"
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$HEALTH_HTTP_TIMEOUT" \
            "$url" 2>/dev/null)" && status=0 || status=$?
    case "$status" in
        0) ;;
        7) echo "kind=connect_refused rc=$status url=$url"; return 1 ;;
        28) echo "kind=timeout rc=$status limit=${HEALTH_HTTP_TIMEOUT}s url=$url"; return 1 ;;
        52|56) echo "kind=empty_or_reset rc=$status url=$url"; return 1 ;;
        *) echo "kind=curl_error rc=$status url=$url"; return 1 ;;
    esac
    case "$code" in
        2??) return 0 ;;
        *) echo "kind=http_status code=$code url=$url"; return 1 ;;
    esac
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

# 洛雪链路：进程层与 HTTP 层分开报。lxserver 与 lxmusic 任一不就绪都要出声，
# 这样 RUNNING + TimeoutError 的场景能在健康检查里留下可导出的证据。
check_lx_http() {
    prog="$1"
    url="$2"
    detail="$(http_ready "$prog" "$url")" || {
        echo "[healthcheck] $prog 进程已运行但 HTTP 未就绪（$detail）" >&2
        return 1
    }
    return 0
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
    elif ! check_lx_http lxserver "$LX_SERVER_HEALTH_URL"; then
        fail=1
    fi
    if ! running lxmusic; then
        echo "[healthcheck] lxmusic 应运行但状态异常" >&2
        fail=1
    elif ! check_lx_http lxmusic "$LX_MUSIC_HEALTH_URL"; then
        fail=1
    fi
fi
check webui FNMUSIC_WEBUI_ENABLED

if [ "$checked" -eq 0 ]; then
    echo "[healthcheck] 未配置任何音源" >&2
    exit 1
fi
exit "$fail"
