#!/bin/sh
# 共享：从 /repo/.env（目录级 bind，始终最新）读布尔开关。
# 优先级：.env 值 > 进程环境变量 > 默认值。
# 被 entrypoint.sh 与 healthcheck.sh source 使用。

ENV_FILE="${FNMUSIC_ENV_FILE:-/repo/.env}"

env_flag() {
    _key="$1"
    _default="${2:-false}"
    _val=""
    if [ -f "$ENV_FILE" ]; then
        _val="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${_key}=" "$ENV_FILE" 2>/dev/null \
            | tail -n1 | cut -d= -f2- \
            | sed -e "s/^['\"]//" -e "s/['\"]$//" -e 's/[[:space:]]*$//')"
    fi
    if [ -z "$_val" ]; then
        _val="$(printenv "$_key" 2>/dev/null || true)"
    fi
    if [ -z "$_val" ]; then
        _val="$_default"
    fi
    case "$_val" in
        true|1|yes|on|TRUE|True) echo "true" ;;
        *) echo "false" ;;
    esac
}

# 从 .env 导出音源运行期变量（已存在的环境变量不覆盖）：
# lx 首次激活的 URL 种子 / 两源平台白名单
# 注意：函数末尾必须以成功命令收尾（调用方 set -e），分支一律用 if 而非 x && y
export_source_env() {
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi
    for _key in LX_SOURCE_URL LX_SOURCES MUSICDL_SOURCES; do
        if printenv "$_key" >/dev/null 2>&1; then
            continue
        fi
        _val="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${_key}=" "$ENV_FILE" 2>/dev/null \
            | tail -n1 | cut -d= -f2- \
            | sed -e "s/^['\"]//" -e "s/['\"]$//" -e 's/[[:space:]]*$//')"
        if [ -n "$_val" ]; then
            export "$_key=$_val"
        fi
    done
    return 0
}
