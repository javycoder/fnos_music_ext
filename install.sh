#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键安装 / 配置
# - 音源可多选、至少选一个：
#     musicdl  https://github.com/CharlesPikachu/musicdl   (:8768)
#     musicbox https://github.com/darknessomi/musicbox     (:8770)
# - 可选开启每日推荐（OpenAI 兼容接口；不填则关闭）
# - 不修改飞牛 nginx / 官方二进制 / 官方数据库写入
# 用法:
#   ./install.sh                         # 交互
#   ./install.sh --mode host
#   ./install.sh --mode docker --sources musicdl,musicbox
#   ./install.sh --non-interactive --mode docker --enable-recommend \
#       --llm-base-url https://api.example.com/v1 --llm-api-key '***' --llm-model gpt-4o-mini
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=""
SOURCES_RAW=""
NON_INTERACTIVE=0
ENABLE_RECOMMEND=""
LLM_BASE_URL=""
LLM_API_KEY=""
LLM_MODEL="gpt-4o-mini"
RUN_EXTEND=0
ENABLE_MUSICDL=0
ENABLE_MUSICBOX=0
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
MUSICDL_REPO="${MUSICDL_REPO:-https://github.com/CharlesPikachu/musicdl}"
MUSICBOX_REPO="${MUSICBOX_REPO:-https://github.com/darknessomi/musicbox}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

usage() {
    cat <<'EOF'
用法: ./install.sh [选项]

  --mode host|docker     安装模式（host=宿主机 venv；docker=音源容器）
  --sources LIST         音源，逗号分隔，可多选，至少选一个
                         取值: musicdl, musicbox（或 1, 2）
                         非交互缺省: musicdl
  --non-interactive      无交互，缺省值：mode=docker，音源=musicdl，不开启每日推荐
  --enable-recommend     开启每日推荐（需同时给 base-url 与 api-key）
  --disable-recommend    明确关闭每日推荐
  --llm-base-url URL     OpenAI 兼容 Base URL，例如 https://api.openai.com/v1
  --llm-api-key KEY      API Key（不会回显；请勿提交到 git）
  --llm-model NAME       模型名，默认 gpt-4o-mini
  --extend               安装完成后立即执行 ./extend.sh
  -h, --help             显示帮助

密钥只写入仓库根目录 .env（chmod 600），不会进入 systemd 文件或日志。
EOF
}

parse_sources() {
    local raw="${1:-}"
    ENABLE_MUSICDL=0
    ENABLE_MUSICBOX=0
    raw="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | tr ' ' ',')"
    local IFS=','
    local part
    # shellcheck disable=SC2086
    for part in ${raw}; do
        part="${part#"${part%%[![:space:]]*}"}"
        part="${part%"${part##*[![:space:]]}"}"
        [ -z "${part}" ] && continue
        case "${part}" in
            1|musicdl|mdl) ENABLE_MUSICDL=1 ;;
            2|musicbox|netease|netease-musicbox) ENABLE_MUSICBOX=1 ;;
            *)
                log_err "未知音源: ${part}（可选 musicdl / musicbox）"
                exit 1
                ;;
        esac
    done
    if [ "${ENABLE_MUSICDL}" -eq 0 ] && [ "${ENABLE_MUSICBOX}" -eq 0 ]; then
        log_err "至少选择一个音源（musicdl / musicbox）"
        exit 1
    fi
}

wait_http() {
    local url="$1" tries="${2:-60}" delay="${3:-2}"
    local i
    for i in $(seq 1 "${tries}"); do
        if curl -sf --max-time 3 "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep "${delay}"
    done
    return 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="${2:-}"; shift 2 ;;
        --sources) SOURCES_RAW="${2:-}"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --enable-recommend) ENABLE_RECOMMEND="yes"; shift ;;
        --disable-recommend) ENABLE_RECOMMEND="no"; shift ;;
        --llm-base-url) LLM_BASE_URL="${2:-}"; shift 2 ;;
        --llm-api-key) LLM_API_KEY="${2:-}"; shift 2 ;;
        --llm-model) LLM_MODEL="${2:-}"; shift 2 ;;
        --extend) RUN_EXTEND=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) log_err "未知参数: $1"; usage; exit 1 ;;
    esac
done

precheck_environment() {
    log_info "==> 开始安装环境预检..."
    local precheck_failed=0

    # 1. 检查 Python 3 与 venv 模块
    if ! command -v python3 >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统未检测到 python3。"
        log_err "请先执行命令安装：sudo apt-get update && sudo apt-get install -y python3 python3-venv"
        precheck_failed=1
    elif ! python3 -c "import venv" >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统 Python 缺少 venv 模块。"
        log_err "请先执行命令安装：sudo apt-get update && sudo apt-get install -y python3-venv"
        precheck_failed=1
    else
        log_info "Python 3 与 venv 模块已就绪。"
    fi

    # 2. 检查 sudo 权限
    if ! sudo -n true 2>/dev/null; then
        if [ -t 0 ]; then
            log_warn "检测到当前操作需要管理员权限，正在请求 sudo 授权..."
            if ! sudo -v; then
                log_err "【权限不足】当前用户无法获取管理员 (sudo) 权限，安装无法继续。"
                precheck_failed=1
            fi
        else
            log_err "【权限不足】非交互模式下需要免密 sudo 权限（sudo -n true 失败）。"
            precheck_failed=1
        fi
    else
        log_info "管理员 (sudo) 权限已就绪。"
    fi

    # 3. 检查飞牛音乐运行套接字
    local target_sock="/var/run/trim_music.socket"
    local upstream_sock="/var/run/trim_music_upstream.socket"
    if [ ! -S "${target_sock}" ] && [ ! -S "${upstream_sock}" ]; then
        log_warn "【前置提醒】未检测到飞牛音乐运行套接字 (${target_sock} 不存在)。"
        log_warn "请确认已在 fnOS 管理界面 ->「应用中心」，安装并启动【飞牛音乐】应用。"
        log_warn "（安装向导仍可继续准备音源依赖与配置，但在最后执行 ./extend.sh 启用扩展前必须先启动飞牛音乐）"
    else
        log_info "飞牛音乐运行套接字检测正常。"
    fi

    # 4. 检查 Docker 环境
    if command -v docker >/dev/null 2>&1; then
        log_info "Docker 容器环境已就绪。"
    else
        log_warn "【提示】系统未检测到 Docker 环境。"
        if [ "${MODE}" = "docker" ]; then
            log_err "【缺少组件】当前指定了 Docker 模式，但系统未安装 Docker。"
            log_err "请先在 fnOS 应用中心安装 Docker，或改用 --mode host 模式。"
            precheck_failed=1
        else
            log_warn "若计划使用 Docker 容器模式运行音源，请先在 fnOS 应用中心安装 Docker；"
            log_warn "您也可以在向导中选择宿主机 (host) 模式直接通过 Python 虚拟环境运行。"
        fi
    fi

    if [ "${precheck_failed}" -ne 0 ]; then
        log_err "环境预检未通过，请处理上述问题后再试。"
        exit 1
    fi
    log_info "环境预检全部通过。"
}

precheck_environment

dotenv_escape() {
    printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

prompt() {
    local msg="$1" def="${2:-}"
    local ans=""
    if [ -n "$def" ]; then
        read -r -p "$msg [$def]: " ans || true
        echo "${ans:-$def}"
    else
        read -r -p "$msg: " ans || true
        echo "$ans"
    fi
}

if [ "${NON_INTERACTIVE}" -eq 0 ]; then
    echo "============================================================"
    echo " fnmusic-ext 安装配置向导"
    echo " 音源: ${MUSICDL_REPO}"
    echo "       ${MUSICBOX_REPO}"
    echo "============================================================"
    if [ -z "${MODE}" ]; then
        echo "请选择安装模式:"
        if command -v docker >/dev/null 2>&1; then
            echo "  1) docker  — 音源以容器运行（推荐，与飞牛系统隔离）"
            echo "  2) host    — 音源以宿主机 Python venv 运行"
            local_choice="$(prompt "输入 1 或 2" "1")"
        else
            echo "  1) docker  — 音源以容器运行（未检测到 Docker，若选此项需先在应用中心安装）"
            echo "  2) host    — 音源以宿主机 Python venv 运行（推荐当前环境）"
            local_choice="$(prompt "输入 1 或 2" "2")"
        fi
        case "${local_choice}" in
            2|host) MODE="host" ;;
            *) MODE="docker" ;;
        esac
    fi
    if [ -z "${SOURCES_RAW}" ]; then
        echo "请选择音源（可多选，逗号分隔，至少选一个）:"
        echo "  1) musicdl   — 聚合音源（酷我/咪咕等，覆盖热门流行）"
        echo "  2) musicbox  — 网易云音源（高品质/无损/歌词封面）"
        echo "  1,2) 全部启用 — 两者兼得，双源并行（推荐）"
        SOURCES_RAW="$(prompt "输入 1 / 2 / 1,2" "1,2")"
    fi
    if [ -z "${ENABLE_RECOMMEND}" ]; then
        echo "大模型每日推荐歌单（可选选填）:"
        echo "  支持接入兼容 OpenAI 协议的大模型（如 DeepSeek/GPT/Qwen 等），"
        echo "  根据播放偏好每天自动生成 20 首推荐新歌。"
        rec_choice="$(prompt "是否开启每日推荐（需 OpenAI 兼容 API Key）? [y/N]" "N")"
        case "${rec_choice}" in
            y|Y|yes|YES) ENABLE_RECOMMEND="yes" ;;
            *) ENABLE_RECOMMEND="no" ;;
        esac
    fi
    if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
        [ -z "${LLM_BASE_URL}" ] && LLM_BASE_URL="$(prompt "LLM Base URL（OpenAI 兼容，例如 https://api.openai.com/v1）")"
        if [ -z "${LLM_API_KEY}" ]; then
            read -r -s -p "LLM API Key（输入不回显，留空则不开启推荐）: " LLM_API_KEY || true
            echo
        fi
        [ -z "${LLM_MODEL}" ] && LLM_MODEL="$(prompt "LLM 模型名" "gpt-4o-mini")"
        LLM_MODEL="${LLM_MODEL:-gpt-4o-mini}"
        if [ -z "${LLM_BASE_URL}" ] || [ -z "${LLM_API_KEY}" ]; then
            log_warn "未同时提供 Base URL 与 API Key，每日推荐将关闭。"
            ENABLE_RECOMMEND="no"
            LLM_BASE_URL=""
            LLM_API_KEY=""
        fi
    fi
    ext_choice="$(prompt "安装配置完成，是否立即执行 extend.sh 启用扩展? [Y/n]" "Y")"
    case "${ext_choice}" in
        n|N|no|NO) RUN_EXTEND=0 ;;
        *) RUN_EXTEND=1 ;;
    esac
else
    MODE="${MODE:-docker}"
    SOURCES_RAW="${SOURCES_RAW:-musicdl}"
    if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
        if [ -z "${LLM_BASE_URL}" ] || [ -z "${LLM_API_KEY}" ]; then
            log_err "--enable-recommend 需要同时提供 --llm-base-url 与 --llm-api-key"
            exit 1
        fi
    else
        ENABLE_RECOMMEND="no"
        LLM_BASE_URL=""
        LLM_API_KEY=""
    fi
fi

MODE="${MODE:-docker}"
if [ "${MODE}" != "host" ] && [ "${MODE}" != "docker" ]; then
    log_err "mode 必须是 host 或 docker"
    exit 1
fi

parse_sources "${SOURCES_RAW}"

SELECTED=""
[ "${ENABLE_MUSICDL}" -eq 1 ] && SELECTED="${SELECTED} musicdl"
[ "${ENABLE_MUSICBOX}" -eq 1 ] && SELECTED="${SELECTED} musicbox"

log_info "安装模式: ${MODE}"
log_info "音源:${SELECTED}"
log_info "每日推荐: ${ENABLE_RECOMMEND}"
log_info "项目目录: ${BASE_DIR}"

mkdir -p "${BASE_DIR}/cache" "${BASE_DIR}/online_favorites" "${BASE_DIR}/play_history" "${BASE_DIR}/recommend_cache"

MUSICDL_FLAG="false"
MUSICBOX_FLAG="false"
[ "${ENABLE_MUSICDL}" -eq 1 ] && MUSICDL_FLAG="true"
[ "${ENABLE_MUSICBOX}" -eq 1 ] && MUSICBOX_FLAG="true"

# --- 写 .env（脱敏：不打印 key） ---
ENV_PATH="${BASE_DIR}/.env"
umask 077
{
    echo "# generated by install.sh — do not commit"
    echo "FNMUSIC_HOME='$(dotenv_escape "${BASE_DIR}")'"
    echo "FNMUSIC_CACHE_DIR='$(dotenv_escape "${BASE_DIR}/cache")'"
    echo "FNMUSIC_FAV_DIR='$(dotenv_escape "${BASE_DIR}/online_favorites")'"
    echo "FNMUSIC_PLAY_HISTORY_DIR='$(dotenv_escape "${BASE_DIR}/play_history")'"
    echo "FNMUSIC_RECOMMEND_DIR='$(dotenv_escape "${BASE_DIR}/recommend_cache")'"
    echo "FNMUSIC_MUSICDL_ENABLED='${MUSICDL_FLAG}'"
    echo "FNMUSIC_NETEASE_ENABLED='${MUSICBOX_FLAG}'"
    echo "FNMUSIC_MUSICDL_URL='http://127.0.0.1:8768'"
    echo "FNMUSIC_MUSICBOX_URL='http://127.0.0.1:8770'"
    echo "FNMUSIC_ONLINE_SOURCES='MiguMusicClient,KuwoMusicClient'"
    if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
        echo "FNMUSIC_LLM_BASE_URL='$(dotenv_escape "${LLM_BASE_URL}")'"
        echo "FNMUSIC_LLM_API_KEY='$(dotenv_escape "${LLM_API_KEY}")'"
        echo "FNMUSIC_LLM_MODEL='$(dotenv_escape "${LLM_MODEL}")'"
    else
        echo "FNMUSIC_LLM_BASE_URL=''"
        echo "FNMUSIC_LLM_API_KEY=''"
        echo "FNMUSIC_LLM_MODEL=''"
    fi
} > "${ENV_PATH}"
chmod 600 "${ENV_PATH}"
log_info "已写入 ${ENV_PATH} (chmod 600)。API Key 不会出现在日志中。"

# --- 代理 Python 环境 ---
if ! command -v python3 >/dev/null 2>&1; then
    log_err "需要 python3"
    exit 1
fi
if [ ! -x "${BASE_DIR}/.venv-proxy/bin/python" ]; then
    log_info "创建 .venv-proxy ..."
    python3 -m venv "${BASE_DIR}/.venv-proxy"
fi
log_info "安装代理依赖..."
"${BASE_DIR}/.venv-proxy/bin/pip" install -q -U pip -i "${PIP_INDEX}"
"${BASE_DIR}/.venv-proxy/bin/pip" install -q -r "${BASE_DIR}/proxy/requirements.txt" pytest -i "${PIP_INDEX}"

install_unit() {
    local src="$1" dest="$2"
    if ! sudo -n true 2>/dev/null; then
        log_warn "无免密 sudo，请手动安装 unit: ${src}"
        log_warn "或稍后用 sudo cp 该文件到 ${dest}"
        return 1
    fi
    sudo cp "${src}" "${dest}"
    rm -f "${src}"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$(basename "${dest}")"
    return 0
}

# --- musicdl ---
install_musicdl_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        log_err "未找到 docker，无法使用 docker 模式。请安装 Docker 或改用 --mode host"
        return 1
    fi
    log_info "构建并启动 musicdl 容器（基于 ${MUSICDL_REPO}）..."
    docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build musicdl
    if wait_http "http://127.0.0.1:8768/healthz" 60 2; then
        log_info "musicdl 已就绪 http://127.0.0.1:8768/healthz"
        return 0
    fi
    log_err "等待 musicdl healthz 超时"
    return 1
}

install_musicdl_host() {
    log_info "宿主机安装 musicdl 服务（pip 包来自 ${MUSICDL_REPO}）..."
    if [ ! -x "${BASE_DIR}/.venv-musicdl/bin/python" ]; then
        python3 -m venv "${BASE_DIR}/.venv-musicdl"
    fi
    "${BASE_DIR}/.venv-musicdl/bin/pip" install -q -U pip -i "${PIP_INDEX}"
    "${BASE_DIR}/.venv-musicdl/bin/pip" install -q -r "${BASE_DIR}/musicdl-service/requirements.txt" -i "${PIP_INDEX}"
    local unit
    unit="$(mktemp)"
    cat > "${unit}" <<EOF
[Unit]
Description=fnmusic-ext musicdl source (${MUSICDL_REPO})
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${BASE_DIR}/musicdl-service
Environment=PYTHONUNBUFFERED=1
Environment=MUSICDL_SOURCES=KuwoMusicClient,MiguMusicClient
Environment=MUSICDL_WORK_DIR=/tmp/musicdl_outputs
ExecStart=${BASE_DIR}/.venv-musicdl/bin/uvicorn app:app --host 127.0.0.1 --port 8768
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    if ! install_unit "${unit}" /etc/systemd/system/fnmusic-musicdl.service; then
        return 0
    fi
    if wait_http "http://127.0.0.1:8768/healthz" 30 1; then
        log_info "宿主机 musicdl 已就绪"
        return 0
    fi
    log_warn "musicdl systemd 已启动，但 healthz 尚未就绪，请检查 journalctl -u fnmusic-musicdl"
}

# --- musicbox ---
install_musicbox_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        log_err "未找到 docker，无法使用 docker 模式。请安装 Docker 或改用 --mode host"
        return 1
    fi
    log_info "构建并启动 musicbox 容器（基于 ${MUSICBOX_REPO}）..."
    docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build musicbox
    if wait_http "http://127.0.0.1:8770/healthz" 60 2; then
        log_info "musicbox 已就绪 http://127.0.0.1:8770/healthz"
        return 0
    fi
    log_err "等待 musicbox healthz 超时"
    return 1
}

install_musicbox_host() {
    log_info "宿主机安装 musicbox 服务（pip 包来自 ${MUSICBOX_REPO}）..."
    mkdir -p "${BASE_DIR}/musicbox-data/cache" "${BASE_DIR}/musicbox-data/config"
    if [ ! -x "${BASE_DIR}/.venv-musicbox/bin/python" ]; then
        python3 -m venv "${BASE_DIR}/.venv-musicbox"
    fi
    "${BASE_DIR}/.venv-musicbox/bin/pip" install -q -U pip -i "${PIP_INDEX}"
    "${BASE_DIR}/.venv-musicbox/bin/pip" install -q -r "${BASE_DIR}/musicbox-service/requirements.txt" -i "${PIP_INDEX}"
    local unit
    unit="$(mktemp)"
    cat > "${unit}" <<EOF
[Unit]
Description=fnmusic-ext musicbox source (${MUSICBOX_REPO})
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${BASE_DIR}/musicbox-service
Environment=PYTHONUNBUFFERED=1
Environment=XDG_DATA_HOME=${BASE_DIR}/musicbox-data
Environment=XDG_CACHE_HOME=${BASE_DIR}/musicbox-data/cache
Environment=XDG_CONFIG_HOME=${BASE_DIR}/musicbox-data/config
ExecStart=${BASE_DIR}/.venv-musicbox/bin/uvicorn app:app --host 127.0.0.1 --port 8770
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    if ! install_unit "${unit}" /etc/systemd/system/fnmusic-musicbox.service; then
        return 0
    fi
    if wait_http "http://127.0.0.1:8770/healthz" 30 1; then
        log_info "宿主机 musicbox 已就绪"
        return 0
    fi
    log_warn "musicbox systemd 已启动，但 healthz 尚未就绪，请检查 journalctl -u fnmusic-musicbox"
}

stop_unselected() {
    if [ "${ENABLE_MUSICDL}" -eq 0 ]; then
        docker rm -f fnmusic-musicdl 2>/dev/null || true
        sudo systemctl disable --now fnmusic-musicdl.service 2>/dev/null || true
    fi
    if [ "${ENABLE_MUSICBOX}" -eq 0 ]; then
        docker rm -f fnmusic-musicbox 2>/dev/null || true
        sudo systemctl disable --now fnmusic-musicbox.service 2>/dev/null || true
    fi
}

if [ "${MODE}" = "docker" ]; then
    [ "${ENABLE_MUSICDL}" -eq 1 ] && install_musicdl_docker
    [ "${ENABLE_MUSICBOX}" -eq 1 ] && install_musicbox_docker
else
    [ "${ENABLE_MUSICDL}" -eq 1 ] && install_musicdl_host
    [ "${ENABLE_MUSICBOX}" -eq 1 ] && install_musicbox_host
fi
stop_unselected

python3 -m py_compile "${BASE_DIR}/proxy/app.py" "${BASE_DIR}/proxy/recommend.py"
bash -n "${BASE_DIR}/extend.sh" "${BASE_DIR}/restore.sh" "${BASE_DIR}/proxy/run_proxy.sh"

log_info "============================================================"
log_info "🎉 fnmusic-ext 安装配置完成！已启用音源:${SELECTED}"
log_info "------------------------------------------------------------"
log_info "【后续验证与使用指引】"
if [ "${RUN_EXTEND}" -eq 1 ]; then
    log_info "即将自动执行 ./extend.sh 进行 Unix Socket 接管与链路自检验收..."
else
    log_info "1. 一键启用扩展："
    log_info "   请在终端运行: ./extend.sh"
    log_info "   （脚本将自动接管 Unix Socket 并进行链路自检验收，安全零侵入）"
fi
log_info "2. 验证搜索与试听："
log_info "   打开飞牛音乐 Web 端或手机 App，在搜索框中搜索歌曲（例如“晴天”或“周杰伦”），"
log_info "   点击在线源歌曲试听，确认可以流畅播放并显示歌词与封面。"
if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    log_info "3. 网易云扫码登录（可选）："
    log_info "   部分网易云 VIP/无损歌曲需要账号凭证，可在局域网浏览器中访问："
    log_info "   http://<NAS_IP>:8770/api/v1/auth/login/qr.png 扫码登录即可。"
fi
if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
    log_info "4. 大模型每日推荐："
    log_info "   已成功配置大模型！登录飞牛音乐后，左侧歌单列表顶部会自动出现「每日推荐」。"
fi
log_info "5. 状态探测与一键还原："
log_info "   • 探测健康状态: curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz"
log_info "   • 随时一键还原: ./restore.sh (立即恢复官方出厂直连状态)"
log_info "============================================================"

if [ "${RUN_EXTEND}" -eq 1 ]; then
    exec "${BASE_DIR}/extend.sh"
fi
