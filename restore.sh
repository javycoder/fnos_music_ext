#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键还原脚本 (Unix Socket 接管架构)
# 功能：停用代理服务并复位 trim-music 原生 Unix Socket
# 参数：--full 额外停止并删除音源容器/宿主机 unit（musicdl、musicbox）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SOCK="/var/run/trim_music.socket"
UPSTREAM_SOCK="/var/run/trim_music_upstream.socket"
FULL_RESTORE=0

for arg in "$@"; do
    case "${arg}" in
        --full)
            FULL_RESTORE=1
            ;;
        -h|--help)
            echo "用法: $0 [--full]"
            echo "  --full: 还原 socket 与代理服务的同时，停止并删除 musicdl/musicbox 容器与宿主机 unit"
            exit 0
            ;;
        *)
            echo "未知参数: ${arg}"
            exit 1
            ;;
    esac
done

log_info() {
    echo -e "\033[32m[INFO]\033[0m $*"
}

log_warn() {
    echo -e "\033[33m[WARN]\033[0m $*"
}

log_err() {
    echo -e "\033[31m[ERROR]\033[0m $*" >&2
}

log_info "==> 开始还原 fnmusic 原生直连模式..."

# 1. sudo -n 检查
if ! sudo -n true 2>/dev/null; then
    log_err "当前用户无法进行无密码 sudo 授权，无法执行还原。"
    exit 1
fi

# 2. 停用并禁用代理服务
log_info "停用并禁用 fnmusic-ext systemd 服务..."
sudo systemctl disable --now fnmusic-ext.service 2>/dev/null || true

# 3. Socket 复位逻辑 (防误删 trim-music 活 socket)
log_info "探测并复位 Unix Socket 状态..."
ORIGINAL_IS_PROXY=0
ORIGINAL_IS_TRIM_MUSIC=0

if [ -S "${TARGET_SOCK}" ]; then
    HEALTH_RESP="$(curl -s --max-time 2 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"
    if echo "${HEALTH_RESP}" | grep -q '"upstream"'; then
        ORIGINAL_IS_PROXY=1
    else
        PROBE_RESP="$(curl -s --max-time 2 --unix-socket "${TARGET_SOCK}" "http://localhost/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
        if echo "${PROBE_RESP}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
            ORIGINAL_IS_TRIM_MUSIC=1
        fi
    fi
fi

if [ "${ORIGINAL_IS_PROXY}" -eq 1 ]; then
    log_info "原路径 (${TARGET_SOCK}) 为代理残留 socket，正在移除并恢复 upstream..."
    sudo rm -f "${TARGET_SOCK}"
    if [ -S "${UPSTREAM_SOCK}" ]; then
        sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
        sudo chmod 666 "${TARGET_SOCK}"
        log_info "已将 upstream 恢复至原路径 (${TARGET_SOCK})。"
    fi
elif [ "${ORIGINAL_IS_TRIM_MUSIC}" -eq 1 ]; then
    log_info "原路径 (${TARGET_SOCK}) 已由 trim-music 直连监听（重启直连场景），保留原 socket，仅清理 upstream 残留..."
    if [ -e "${UPSTREAM_SOCK}" ] || [ -S "${UPSTREAM_SOCK}" ]; then
        sudo rm -f "${UPSTREAM_SOCK}"
    fi
    sudo chmod 666 "${TARGET_SOCK}" 2>/dev/null || true
else
    log_info "原路径 (${TARGET_SOCK}) 无有效响应或文件不存在，正在复位..."
    sudo rm -f "${TARGET_SOCK}" 2>/dev/null || true
    if [ -S "${UPSTREAM_SOCK}" ]; then
        sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
        sudo chmod 666 "${TARGET_SOCK}"
        log_info "已将 ${UPSTREAM_SOCK} 移动至 ${TARGET_SOCK}。"
    else
        log_warn "未发现可恢复的 upstream socket，如果飞牛音乐无法连接，请在飞牛系统管理中重启飞牛音乐。"
    fi
fi

# 4. 验证直连恢复
log_info "验证直连链路..."
url_5667="https://127.0.0.1:5667/music/api/v1/search/track?keyword=test"
url_443="https://127.0.0.1/music/api/v1/search/track?keyword=test"

verify_resp="$(curl -sk --max-time 5 "${url_5667}" 2>/dev/null || true)"
if [ -z "${verify_resp}" ]; then
    verify_resp="$(curl -skL --max-time 5 "${url_443}" 2>/dev/null || true)"
fi

if echo "${verify_resp}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
    log_info "直连验证成功: 官方 trim-music 正常响应 (INVALID TOKEN)。"
else
    log_warn "直连验证未收到预期响应: ${verify_resp:-无响应}"
fi

# 5. full 模式额外清理音源
if [ "${FULL_RESTORE}" -eq 1 ]; then
    log_info "(--full 模式) 停止并移除音源容器与宿主机 unit..."
    docker rm -f fnmusic-musicdl fnmusic-musicbox 2>/dev/null || true
    sudo systemctl disable --now fnmusic-musicdl.service 2>/dev/null || true
    sudo systemctl disable --now fnmusic-musicbox.service 2>/dev/null || true
    log_info "musicdl / musicbox 已停止。"
else
    log_info "默认保留音源容器/unit 与 cache/ 目录。"
fi

# 6. 移除 systemd unit
if [ -f "/etc/systemd/system/fnmusic-ext.service" ]; then
    log_info "移除 /etc/systemd/system/fnmusic-ext.service..."
    sudo rm -f "/etc/systemd/system/fnmusic-ext.service"
    sudo systemctl daemon-reload 2>/dev/null || true
fi

log_info "============================================================"
log_info "fnmusic 已成功还原为原生直连模式！"
log_info "============================================================"
