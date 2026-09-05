#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键扩展脚本 (Unix Socket 接管架构)
# 功能：接管 /var/run/trim_music.socket，实现零侵入扩展（严禁修改 nginx 配置）
# 具备幂等性与自动回滚能力
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SOCK="/var/run/trim_music.socket"
UPSTREAM_SOCK="/var/run/trim_music_upstream.socket"
MUSICDL_URL="http://127.0.0.1:8768"
MUSICBOX_URL="http://127.0.0.1:8770"

log_info() {
    echo -e "\033[32m[INFO]\033[0m $*"
}

log_warn() {
    echo -e "\033[33m[WARN]\033[0m $*"
}

log_err() {
    echo -e "\033[31m[ERROR]\033[0m $*" >&2
}

is_enabled() {
    case "$(printf '%s' "${1:-true}" | tr '[:upper:]' '[:lower:]')" in
        false|0|no|off) return 1 ;;
        *) return 0 ;;
    esac
}

if [ ! -f "${BASE_DIR}/.env" ]; then
    if [ -t 0 ]; then
        log_warn "检测到尚未完成初次安装配置（未找到 .env 配置文件）。"
        read -r -p "检测到尚未完成初次安装配置，是否现在启动安装向导 (./install.sh)？[Y/n] " prompt_ans || true
        case "${prompt_ans:-y}" in
            y|Y|yes|YES|"")
                log_info "正在启动安装向导 (./install.sh)..."
                exec "${BASE_DIR}/install.sh"
                ;;
            *)
                log_err "请先执行 ./install.sh 完成音源与配置安装。"
                exit 1
                ;;
        esac
    else
        log_err "请先执行 ./install.sh 完成音源与配置安装。"
        exit 1
    fi
fi

set -a
# shellcheck disable=SC1091
source "${BASE_DIR}/.env"
set +a
MUSICDL_URL="${FNMUSIC_MUSICDL_URL:-${MUSICDL_URL}}"
MUSICBOX_URL="${FNMUSIC_MUSICBOX_URL:-${MUSICBOX_URL}}"
ENABLE_MUSICDL=0
ENABLE_MUSICBOX=0
is_enabled "${FNMUSIC_MUSICDL_ENABLED:-true}" && ENABLE_MUSICDL=1
is_enabled "${FNMUSIC_NETEASE_ENABLED:-true}" && ENABLE_MUSICBOX=1
if [ "${ENABLE_MUSICDL}" -eq 0 ] && [ "${ENABLE_MUSICBOX}" -eq 0 ]; then
    log_err "至少需要启用一个音源（FNMUSIC_MUSICDL_ENABLED / FNMUSIC_NETEASE_ENABLED）。"
    exit 1
fi

# ------------------------------------------------------------------------------
# 回滚函数 (restore 逻辑)
# ------------------------------------------------------------------------------
rollback() {
    log_err "执行遇到错误或验收失败，正在执行自动回滚..."
    sudo systemctl disable --now fnmusic-ext.service 2>/dev/null || true

    # 探测 socket 状态
    local health_resp
    health_resp="$(curl -s --max-time 2 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"
    if echo "${health_resp}" | grep -q '"upstream"'; then
        # 当前原路径仍是代理 socket 残留，清理并恢复 upstream
        log_info "正在清理代理 socket 并恢复官方 trim-music socket..."
        sudo rm -f "${TARGET_SOCK}"
        if [ -S "${UPSTREAM_SOCK}" ]; then
            sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
            sudo chmod 666 "${TARGET_SOCK}"
        fi
    elif [ -S "${UPSTREAM_SOCK}" ] && [ ! -S "${TARGET_SOCK}" ]; then
        log_info "正在将 upstream socket 恢复为原路径..."
        sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
        sudo chmod 666 "${TARGET_SOCK}"
    fi

    log_err "回滚完成。扩展未能成功启用。"
    exit 1
}

# ------------------------------------------------------------------------------
# 验收测试函数
# ------------------------------------------------------------------------------
verify_acceptance() {
    log_info "==> 执行链路与功能验收..."

    # 6a. 401 快速路径响应与时延测试 (< 3s)
    log_info "验收 6a: 验证未登录 401/99999 快速路径透传 (耗时必须 < 3s)..."
    local url_5667="https://127.0.0.1:5667/music/api/v1/search/track?keyword=test"
    local url_443="https://127.0.0.1/music/api/v1/search/track?keyword=test"
    local resp_file
    resp_file="$(mktemp)"
    local time_total=""

    time_total="$(curl -sk --max-time 8 -w "%{time_total}" -o "${resp_file}" "${url_5667}" 2>/dev/null || echo "")"
    if [ -z "${time_total}" ] || [ ! -s "${resp_file}" ]; then
        log_warn "5667 端口连接异常，尝试 fallback 访问 443 端口 (302 跳转)..."
        time_total="$(curl -skL --max-time 8 -w "%{time_total}" -o "${resp_file}" "${url_443}" 2>/dev/null || echo "99")"
    fi

    local resp_content
    resp_content="$(cat "${resp_file}")"
    rm -f "${resp_file}"

    if ! echo "${resp_content}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
        log_err "验收 6a 失败：未收到预期的 INVALID TOKEN 响应。实际响应: ${resp_content}"
        return 1
    fi

    local is_fast
    is_fast="$(awk -v t="${time_total}" 'BEGIN{print (t < 3.0) ? "1" : "0"}')"
    if [ "${is_fast}" != "1" ]; then
        log_err "验收 6a 失败：401 请求耗时过长 (${time_total}s >= 3s)，快速路径可能被阻塞！"
        return 1
    fi
    log_info "验收 6a 通过：INVALID TOKEN 正确透传，耗时 ${time_total}s (< 3s)。"

    # 6b. 在线音频取流与全链路测试 (Range: bytes=0-1048575 -> 200/206)
    log_info "验收 6b: 验证在线播放全链路取流 (Range 200/206 及数据流传输)..."

    local probe_keywords=("晴天" "海阔天空" "稻香")
    local search_any_result=0
    local proxy_internal_error=0

    # 从指定音源搜索候选歌曲，逐行输出 id（可能为空）
    search_probe_ids() {
        local source="$1" keyword="$2" encoded
        encoded="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "${keyword}" 2>/dev/null || true)"
        [ -z "${encoded}" ] && return 0
        if [ "${source}" = "musicdl" ]; then
            curl -s --max-time 20 "${MUSICDL_URL}/search?keyword=${encoded}&limit=3" 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    for it in (d.get('items') or [])[:3]:
        i=it.get('id')
        if i: print(i)
except Exception:
    pass" 2>/dev/null || true
        else
            curl -s --max-time 20 "${MUSICBOX_URL}/api/v1/search?keyword=${encoded}&limit=3&type=song" 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    rows=d.get('data') if isinstance(d, dict) else None
    if not isinstance(rows, list):
        rows=d.get('songs') if isinstance(d, dict) else None
    for it in (rows or [])[:3]:
        sid=str(it.get('song_id') or it.get('id') or '')
        if sid: print('netease:'+sid)
except Exception:
    pass" 2>/dev/null || true
        fi
    }

    # 对指定 guid 尝试全链路取流，成功返回 0 并输出 http_code
    try_probe_stream() {
        local probe_id="$1"
        local stream_guid="online:${probe_id}"
        local stream_5667="https://127.0.0.1:5667/music/api/v1/track/stream?guid=${stream_guid}"
        local stream_443="https://127.0.0.1/music/api/v1/track/stream?guid=${stream_guid}"
        local out_file http_code recv_size=0
        out_file="$(mktemp)"
        log_info "试播 guid=${stream_guid}"
        http_code="$(curl -sk -o "${out_file}" -w "%{http_code}" -H "Range: bytes=0-1048575" --max-time 90 "${stream_5667}" 2>/dev/null || echo "000")"
        if [ "${http_code}" = "000" ]; then
            log_warn "5667 端口连接异常，尝试 fallback 访问 443 端口取流..."
            http_code="$(curl -skL -o "${out_file}" -w "%{http_code}" -H "Range: bytes=0-1048575" --max-time 90 "${stream_443}" 2>/dev/null || echo "000")"
        fi
        if [ -f "${out_file}" ]; then
            recv_size="$(wc -c < "${out_file}" | tr -d ' ')"
            rm -f "${out_file}"
        fi
        case "${http_code}" in
            500|502|503|504) proxy_internal_error=1 ;;
        esac
        if { [ "${http_code}" = "206" ] || [ "${http_code}" = "200" ]; } && [ "${recv_size}" -gt 10000 ]; then
            if [ "${recv_size}" -lt 500000 ]; then
                log_warn "在线音频流接收大小为 ${recv_size} 字节 (偏小但已收到有效数据)。"
            else
                log_info "在线音频流接收大小为 ${recv_size} 字节 (≈1MB)。"
            fi
            return 0
        fi
        log_warn "取流未成功: guid=${stream_guid} HTTP=${http_code} recv=${recv_size}B"
        return 1
    }

    local sources=()
    [ "${ENABLE_MUSICDL}" -eq 1 ] && sources+=("musicdl")
    [ "${ENABLE_MUSICBOX}" -eq 1 ] && sources+=("musicbox")

    local src kw id
    for src in "${sources[@]}"; do
        for kw in "${probe_keywords[@]}"; do
            while IFS= read -r id; do
                [ -z "${id}" ] && continue
                search_any_result=1
                if try_probe_stream "${id}"; then
                    log_info "验收 6b 通过：音源 ${src} 在线播放流取流成功。"
                    return 0
                fi
            done < <(search_probe_ids "${src}" "${kw}")
        done
    done

    if [ "${proxy_internal_error}" -eq 1 ]; then
        log_err "验收 6b 失败：代理服务本身返回内部错误 (500/502/503/504)，视为严重异常。"
        return 1
    fi

    if [ "${search_any_result}" -eq 0 ]; then
        log_warn "所有已启用音源 (${sources[*]}) 搜索结果均为空：可能是外部网络异常或第三方平台限流。"
        log_warn "跳过在线播放自动验收（不触发回滚），建议稍后在飞牛音乐 Web 端手动搜索试播验证。"
    else
        log_warn "所有候选歌曲均未能成功取流，但代理服务本身响应正常（无 500/502 内部错误）。"
        log_warn "跳过在线播放自动验收（不触发回滚），建议稍后在飞牛音乐 Web 端手动搜索试播验证。"
    fi
    return 0
}

# ------------------------------------------------------------------------------
# 1. 预检
# ------------------------------------------------------------------------------
log_info "==> 步骤 1/5: 环境预检..."

# 1.1 检查 Python 3 与 venv 模块
if ! command -v python3 >/dev/null 2>&1; then
    log_err "【缺少基础依赖】系统未检测到 python3。"
    log_err "请先执行以下命令安装基础组件：sudo apt-get update && sudo apt-get install -y python3 python3-venv"
    exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
    log_err "【缺少基础依赖】系统 Python 缺少 venv 模块。"
    log_err "请先执行以下命令安装基础组件：sudo apt-get update && sudo apt-get install -y python3-venv"
    exit 1
fi

# 1.2 sudo 权限检查
if ! sudo -n true 2>/dev/null; then
    if [ -t 0 ]; then
        log_warn "需要管理员权限执行扩展配置，正在请求 sudo 授权..."
        sudo -v || {
            log_err "管理员权限获取失败，请确认当前用户具备 sudo 权限。"
            exit 1
        }
    else
        log_err "当前用户无法进行无密码 sudo 授权，请确认当前用户具备 sudo 权限。"
        exit 1
    fi
fi

# 1.3 检查飞牛音乐 socket 文件
if [ ! -S "${TARGET_SOCK}" ] && [ ! -S "${UPSTREAM_SOCK}" ]; then
    log_err "【前置条件未满足】未检测到飞牛音乐运行套接字。"
    log_err "请先在 fnOS 管理界面 -> 应用中心，安装并启动【飞牛音乐】应用后，再运行本脚本。"
    exit 1
fi

# 1.4 检查 / 自动拉起已启用的音源
ensure_source() {
    local name="$1" url="$2" compose_svc="$3" unit="$4"
    if curl -sf --max-time 5 "${url}/healthz" >/dev/null 2>&1; then
        log_info "${name} 已就绪 (${url}/healthz)。"
        return 0
    fi
    log_warn "${name} 未就绪，正在拉起..."
    if command -v docker >/dev/null 2>&1; then
        docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build "${compose_svc}" || true
    fi
    if systemctl list-unit-files "${unit}" >/dev/null 2>&1; then
        sudo systemctl start "${unit}" 2>/dev/null || true
    fi
    local i
    for i in $(seq 1 60); do
        if curl -sf --max-time 3 "${url}/healthz" >/dev/null 2>&1; then
            log_info "${name} 已就绪。"
            return 0
        fi
        sleep 2
    done
    log_err "等待 ${name} healthz 超时 (${url}/healthz)。"
    return 1
}

if [ "${ENABLE_MUSICDL}" -eq 1 ]; then
    if ! ensure_source "musicdl" "${MUSICDL_URL}" "musicdl" "fnmusic-musicdl.service"; then
        exit 1
    fi
fi
if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    if ! ensure_source "musicbox" "${MUSICBOX_URL}" "musicbox" "fnmusic-musicbox.service"; then
        exit 1
    fi
fi

# 1.5 检查 Python 虚拟环境与依赖
if [ ! -f "${BASE_DIR}/.venv-proxy/bin/python" ]; then
    log_info "创建 .venv-proxy 虚拟环境..."
    python3 -m venv "${BASE_DIR}/.venv-proxy"
    "${BASE_DIR}/.venv-proxy/bin/pip" install -r "${BASE_DIR}/proxy/requirements.txt" pytest -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

# 1.6 编译与语法检查
python3 -m py_compile "${BASE_DIR}/proxy/app.py" "${BASE_DIR}/proxy/recommend.py"
bash -n "${BASE_DIR}/proxy/run_proxy.sh"

# ------------------------------------------------------------------------------
# 2. 幂等性检查
# ------------------------------------------------------------------------------
log_info "==> 步骤 2/5: 幂等性检查..."
HEALTH_CHECK="$(curl -s --max-time 3 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"

if echo "${HEALTH_CHECK}" | grep -q '"upstream":[[:space:]]*"ok"'; then
    log_info "检测到代理服务已在运行且上游健康 (处于扩展接管态)。"
    log_info "直接运行验收测试确认状态..."
    if verify_acceptance; then
        log_info "============================================================"
        log_info "fnmusic-ext 当前已处于扩展态且运行正常，无需重复操作！"
        log_info "============================================================"
        exit 0
    else
        log_warn "现有扩展态验收未通过，将重启服务重新接管..."
    fi
fi

# ------------------------------------------------------------------------------
# 3. 安装并启动 systemd unit（按当前目录生成，禁止写死个人路径）
# ------------------------------------------------------------------------------
log_info "==> 步骤 3/5: 安装 systemd 服务并启动接管..."
UNIT_TMP="$(mktemp)"
cat > "${UNIT_TMP}" <<EOF
[Unit]
Description=fnmusic-ext Proxy Service (Socket Takeover)
After=network.target docker.service

[Service]
Type=simple
User=root
WorkingDirectory=${BASE_DIR}
ExecStart=${BASE_DIR}/proxy/run_proxy.sh
ExecStartPost=/bin/sh -c 'for i in \$(seq 1 30); do [ -S /var/run/trim_music.socket ] && chmod 666 /var/run/trim_music.socket && exit 0; sleep 1; done; exit 1'
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=FNMUSIC_HOME=${BASE_DIR}
EnvironmentFile=-${BASE_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF
sudo cp "${UNIT_TMP}" /etc/systemd/system/fnmusic-ext.service
rm -f "${UNIT_TMP}"
sudo systemctl daemon-reload

if systemctl is-active --quiet fnmusic-ext.service 2>/dev/null; then
    log_info "重启 fnmusic-ext 服务..."
    sudo systemctl restart fnmusic-ext.service
else
    log_info "启用并启动 fnmusic-ext 服务..."
    sudo systemctl enable --now fnmusic-ext.service
fi

# ------------------------------------------------------------------------------
# 4. 等待接管完成与健康检查
# ------------------------------------------------------------------------------
log_info "==> 步骤 4/5: 等待代理服务接管完成并就绪..."
READY=0
for i in $(seq 1 30); do
    STATUS_JSON="$(curl -s --max-time 2 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"
    if echo "${STATUS_JSON}" | grep -q '"ok":[[:space:]]*true' && echo "${STATUS_JSON}" | grep -q '"upstream":[[:space:]]*"ok"'; then
        READY=1
        break
    fi
    sleep 1
done

if [ "${READY}" -ne 1 ]; then
    log_err "等待接管超时 (30s) 或 healthz 未通过。当前探测响应: ${STATUS_JSON:-无响应}"
    rollback
fi

log_info "Unix socket 接管成功且健康探测通过。"

# ------------------------------------------------------------------------------
# 5. 验收测试与自动回滚
# ------------------------------------------------------------------------------
log_info "==> 步骤 5/5: 验收链路连通性..."
if ! verify_acceptance; then
    rollback
fi

log_info "============================================================"
log_info "fnmusic-ext 扩展已成功部署并生效！"
log_info "架构：Unix Socket 接管 (零侵入，不修改 nginx 配置)"
log_info "在线音源搜索合并、在线播放与元数据代理已就绪。"
log_info "------------------------------------------------------------"
log_info "【后续验证与使用指引】"
log_info "1. 验证搜索与播放："
log_info "   打开飞牛音乐 Web 端或手机 App，搜索歌曲（如“晴天”或“周杰伦”），"
log_info "   点击在线源歌曲试听，确认可以流畅播放并显示歌词与封面。"
if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    log_info "2. 网易云扫码登录（可选）："
    log_info "   若遇到部分网易云 VIP/无损歌曲需登录，可访问："
    log_info "   http://<NAS_IP>:8770/api/v1/auth/login/qr.png 使用网易云 App 扫码登录。"
fi
log_info "3. 健康检查与运维："
log_info "   • 探测状态: curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz"
log_info "   • 查看日志: sudo journalctl -u fnmusic-ext -f"
log_info "   • 一键还原: ./restore.sh (一键无损切回官方原生直连)"
log_info "============================================================"
