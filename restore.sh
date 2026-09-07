#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键还原脚本 (Unix Socket 接管架构)
# 功能：停用代理服务并复位 trim-music 原生 Unix Socket
# 参数：--full 额外停止并删除音源容器/宿主机 unit（musicdl、musicbox、lxmusic）
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

# ------------------------------------------------------------------------------
# 获取 fnOS 网关 http/https 端口 (读取失败时默认 5666/5667)
# 输出: "<http_port> <https_port>"
# ------------------------------------------------------------------------------
get_fnos_gateway_ports() {
    cat /usr/trim/etc/network_gateway_setting.conf 2>/dev/null | python3 -c '
import sys, json, re
text = sys.stdin.read()
http_port, https_port = "5666", "5667"
def scan(obj):
    global http_port, https_port
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, (int, str)) and str(v).isdigit():
                if "https" in kl and "port" in kl:
                    https_port = str(v)
                elif "http" in kl and "port" in kl:
                    http_port = str(v)
            else:
                scan(v)
    elif isinstance(obj, list):
        for it in obj:
            scan(it)
if text.strip():
    try:
        scan(json.loads(text))
    except Exception:
        pass
    if http_port == "5666":
        m = re.search(r"\"?http_port\"?\s*[:=]\s*(\d+)", text)
        if m:
            http_port = m.group(1)
    if https_port == "5667":
        m = re.search(r"\"?https_port\"?\s*[:=]\s*(\d+)", text)
        if m:
            https_port = m.group(1)
print(http_port, https_port)
' 2>/dev/null || echo "5666 5667"
}

probe_socket_identity() {
    # 在停代理之前探测：1=proxy 2=trim-music 0=unknown/absent
    local sock="$1"
    if [ ! -S "${sock}" ]; then
        echo "absent"
        return 0
    fi
    local health_resp probe_resp
    health_resp="$(curl -s --max-time 2 --unix-socket "${sock}" http://localhost/_ext/healthz 2>/dev/null || true)"
    if echo "${health_resp}" | grep -q '"upstream"'; then
        echo "proxy"
        return 0
    fi
    probe_resp="$(curl -s --max-time 2 --unix-socket "${sock}" "http://localhost/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
    if echo "${probe_resp}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
        echo "trim-music"
        return 0
    fi
    echo "unknown"
}

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

# 2. 停代理前完成身份探测（避免停服务后探针误判）
log_info "探测 Unix Socket 身份（停代理前）..."
TARGET_IDENTITY="$(probe_socket_identity "${TARGET_SOCK}")"
UPSTREAM_EXISTS=0
[ -S "${UPSTREAM_SOCK}" ] && UPSTREAM_EXISTS=1
log_info "原路径身份: ${TARGET_IDENTITY}；upstream 存在: ${UPSTREAM_EXISTS}"

# 3. 停用并禁用代理服务
log_info "停用并禁用 fnmusic-ext systemd 服务..."
sudo systemctl disable --now fnmusic-ext.service 2>/dev/null || true

# 4. Socket 复位逻辑（绝不误删官方活 socket）
log_info "复位 Unix Socket 状态..."
case "${TARGET_IDENTITY}" in
    proxy)
        log_info "原路径 (${TARGET_SOCK}) 为代理 socket，正在移除并恢复 upstream..."
        sudo rm -f "${TARGET_SOCK}"
        if [ -S "${UPSTREAM_SOCK}" ]; then
            sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
            sudo chmod 666 "${TARGET_SOCK}"
            log_info "已将 upstream 恢复至原路径 (${TARGET_SOCK})。"
        else
            log_warn "未发现 upstream socket。若飞牛音乐无响应，请在应用中心重启飞牛音乐以重建官方 socket。"
        fi
        ;;
    trim-music)
        log_info "原路径 (${TARGET_SOCK}) 已由 trim-music 直连监听，保留原 socket，仅清理 upstream 残留..."
        if [ -e "${UPSTREAM_SOCK}" ] || [ -S "${UPSTREAM_SOCK}" ]; then
            sudo rm -f "${UPSTREAM_SOCK}"
        fi
        sudo chmod 666 "${TARGET_SOCK}" 2>/dev/null || true
        ;;
    absent)
        if [ -S "${UPSTREAM_SOCK}" ]; then
            log_info "原路径不存在，将 upstream 移回原路径..."
            sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
            sudo chmod 666 "${TARGET_SOCK}"
        else
            log_warn "原路径与 upstream 均不存在。请在应用中心重启飞牛音乐以重建官方 socket。"
        fi
        ;;
    unknown|*)
        # 保守策略：不确定时绝不删除 TARGET_SOCK，以免误伤官方活 socket
        if [ -S "${UPSTREAM_SOCK}" ] && [ ! -S "${TARGET_SOCK}" ]; then
            log_info "原路径无有效 socket，将 upstream 移回..."
            sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
            sudo chmod 666 "${TARGET_SOCK}"
        elif [ -S "${UPSTREAM_SOCK}" ] && [ -S "${TARGET_SOCK}" ]; then
            log_warn "原路径 socket 身份不明且 upstream 仍存在：保留原路径，不删除。"
            log_warn "若确认扩展残留，可重启飞牛音乐后再执行本脚本。"
        else
            log_warn "原路径 socket 身份不明（可能为官方服务暂未就绪）。保留现有文件，不做删除。"
            log_warn "若飞牛音乐无法连接，请在应用中心重启飞牛音乐。"
        fi
        ;;
esac

# 5. 验证直连恢复 (优先 Unix socket 探活，失败再走网关 https 端口 / 443)
log_info "验证直连链路..."
GW_HTTP_PORT=""
GW_HTTPS_PORT=""
read -r GW_HTTP_PORT GW_HTTPS_PORT <<< "$(get_fnos_gateway_ports)"
log_info "fnOS 网关端口: http=${GW_HTTP_PORT} https=${GW_HTTPS_PORT}"

verify_resp="$(curl -s --max-time 5 --unix-socket "${TARGET_SOCK}" "http://localhost/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
if [ -z "${verify_resp}" ]; then
    log_warn "socket 探测异常，尝试 fallback 访问网关 https 端口 (${GW_HTTPS_PORT})..."
    verify_resp="$(curl -sk --max-time 5 "https://127.0.0.1:${GW_HTTPS_PORT}/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
fi
if [ -z "${verify_resp}" ]; then
    verify_resp="$(curl -skL --max-time 5 "https://127.0.0.1/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
fi

if echo "${verify_resp}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
    log_info "直连验证成功: 官方 trim-music 正常响应 (INVALID TOKEN)。"
else
    log_warn "直连验证未收到预期响应: ${verify_resp:-无响应}"
fi

# 6. full 模式额外清理音源
if [ "${FULL_RESTORE}" -eq 1 ]; then
    log_info "(--full 模式) 停止并移除音源容器与宿主机 unit..."
    rm_out=""
    if ! rm_out="$(run_docker rm -f fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic 2>&1)"; then
        log_warn "音源容器移除出现错误（可能部分未删除）: ${rm_out}"
    fi
    if [ -f "${BASE_DIR}/docker-compose.yml" ]; then
        (cd "${BASE_DIR}" && run_docker compose -f docker-compose.yml down --remove-orphans 2>/dev/null) || true
    fi
    for unit in fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic; do
        sudo systemctl disable --now "${unit}.service" 2>/dev/null || true
        if [ -f "/etc/systemd/system/${unit}.service" ]; then
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
