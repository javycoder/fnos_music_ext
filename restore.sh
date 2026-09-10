#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键还原脚本 (Unix Socket 接管架构)
# 功能：停用代理服务并复位 trim-music 原生 Unix Socket
# 参数：--full 额外停止并删除音源容器/宿主机 unit（musicdl、musicbox、lxmusic）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared outer lock is never acquired by systemd service helpers.
source "${BASE_DIR}/proxy/install_common.sh"
installation_lock "$@"
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
            echo "  --full: 还原 socket 与代理服务的同时，停止并删除 musicdl/musicbox/lxmusic 容器与宿主机 unit"
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

run_docker() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        sudo docker "$@"
    else
        return 1
    fi
}

check_proxy_unit_owner || exit 1

log_info "==> 开始还原 fnmusic 原生直连模式..."

# 1. sudo 权限检查
if ! sudo -n true 2>/dev/null; then
    if [ -t 0 ]; then
        log_warn "需要管理员权限执行还原，正在请求 sudo 授权..."
        sudo -v || {
            log_err "管理员权限获取失败，请确认当前用户具备 sudo 权限。"
            exit 1
        }
    else
        log_err "当前用户无法进行无密码 sudo 授权，无法执行还原。"
        exit 1
    fi
fi

# Persist live identity BEFORE stop; supervisor and ExecStopPost use the same state.
log_info "记录 socket 身份并停止代理..."
takeover remember
if [ -f /etc/systemd/system/fnmusic-ext.service ]; then
    sudo systemctl disable --now fnmusic-ext.service
fi
if ! takeover restore; then
    log_err "未能验证官方直连恢复；保留未知 socket，不宣称成功。请排查冲突后重试。"
    exit 1
fi

# 6. full 模式额外清理音源
if [ "${FULL_RESTORE}" -eq 1 ]; then
    log_info "(--full 模式) 停止并移除音源容器与宿主机 unit..."
    for unit in fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic; do
        remove_owned_container "${unit}"
        if owned_source_unit "${unit}"; then
            stop_owned_source_unit "${unit}"
            sudo rm -f "/etc/systemd/system/${unit}.service"
        fi
    done
    # 如实校验清理结果：容器可能被其他副本/并发任务重建，绝不静默假成功
    leftover=""
    for unit in fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic; do
        if run_docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${unit}"; then
            leftover="${leftover} ${unit}"
        fi
    done
    if [ -n "${leftover}" ]; then
        log_warn "以下容器仍存在（可能刚被其他副本或并发任务重建）:${leftover}"
        log_warn "如需彻底清理，请手动执行: docker rm -f${leftover}"
    else
        log_info "musicdl / musicbox / lxmusic 容器与宿主机 unit 已清理完毕。"
    fi
else
    log_info "默认保留音源容器/unit 与 cache/ 目录。"
fi

# 7. 移除代理 systemd unit
if [ -f "/etc/systemd/system/fnmusic-ext.service" ]; then
    log_info "移除 /etc/systemd/system/fnmusic-ext.service..."
    sudo rm -f "/etc/systemd/system/fnmusic-ext.service"
fi
sudo systemctl daemon-reload 2>/dev/null || true

log_info "============================================================"
log_info "fnmusic 已成功还原为原生直连模式！"
log_info "============================================================"
