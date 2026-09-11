#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键还原脚本 (Unix Socket 接管架构)
# 功能：停用代理服务并复位 trim-music 原生 Unix Socket
# 语义：
#   默认     还原官方直连 + 移除代理 unit + 停止并删除音源容器/宿主机 unit；
#            .env 与全部数据（网易云登录、缓存、在线收藏、播放历史、推荐缓存）保留，
#            重装后无需重新填写大模型 Key 等任何配置。
#   --full   在默认动作之外执行工厂级清理：删除 .env（含历史备份）与上述全部数据
#            目录及虚拟环境；仅保留代码与 git 仓库本身。
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
            echo "  默认:   还原官方直连，删除代理 unit 与音源容器/宿主机 unit；保留 .env 与全部数据"
            echo "  --full: 额外删除 .env（含备份）、网易云登录、缓存、在线收藏、播放历史、"
            echo "          推荐缓存与虚拟环境（保留代码），用于彻底重置"
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

# 工厂级清理：删除配置与数据（代码与 git 保留）。仅允许清理 BASE_DIR 内的已知路径。
purge_local_state() {
    local removed="" target
    for target in "${BASE_DIR}"/.env "${BASE_DIR}"/.env.bak.* \
                  "${BASE_DIR}/musicbox-data" "${BASE_DIR}/cache" \
                  "${BASE_DIR}/online_favorites" "${BASE_DIR}/play_history" \
                  "${BASE_DIR}/recommend_cache" "${BASE_DIR}"/.venv-*; do
        # 通配符未匹配时会原样出现，且仅允许清理本目录内的路径
        if [ -e "${target}" ] && [[ "${target}" == "${BASE_DIR}"/* ]]; then
            rm -rf -- "${target}"
            removed="${removed} ${target#"${BASE_DIR}/"}"
        fi
    done
    if [ -n "${removed}" ]; then
        log_info "已删除:${removed}"
    fi
    log_info "已保留：代码、git 仓库与 Docker 镜像（重装时可直接复用缓存）。"
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

# Verify recoverability BEFORE any stop: unsupported legacy layout must fail
# while the proxy is still running, never after disabling it.
if ! plan_json="$(takeover restore-plan)"; then
    log_err "当前 socket 状态不支持已验证的恢复，未停止任何服务。"
    log_err "请检查是否有其他副本占用、旧版代理无身份接口，或官方应用需要重启后重试。"
    exit 1
fi
log_info "恢复预检通过 (${plan_json})，记录 socket 身份并停止代理..."
takeover remember
if [ -f /etc/systemd/system/fnmusic-ext.service ]; then
    sudo systemctl disable --now fnmusic-ext.service
fi
if ! takeover restore; then
    log_err "未能验证官方直连恢复；保留未知 socket，不宣称成功。请排查冲突后重试。"
    exit 1
fi

# 2. 移除代理 systemd unit
if [ -f "/etc/systemd/system/fnmusic-ext.service" ]; then
    log_info "移除 /etc/systemd/system/fnmusic-ext.service..."
    sudo rm -f /etc/systemd/system/fnmusic-ext.service
fi
sudo systemctl daemon-reload 2>/dev/null || true

# 3. 停止并移除音源容器与宿主机 unit（默认动作；配置与数据一律保留）
log_info "停止并移除音源容器与宿主机 unit..."
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

# 4. --full：工厂级清理配置与数据（代码与 git 保留）
if [ "${FULL_RESTORE}" -eq 1 ]; then
    log_info "(--full 模式) 删除配置与数据（.env、网易云登录、缓存、收藏、历史、推荐缓存、虚拟环境）..."
    purge_local_state
else
    log_info "已保留 .env 与全部数据（网易云登录/缓存/在线收藏/播放历史/推荐缓存）。"
    log_info "如需彻底重置（删除全部配置与数据），请执行: $0 --full"
fi

log_info "============================================================"
log_info "fnmusic 已成功还原为原生直连模式！"
log_info "============================================================"
