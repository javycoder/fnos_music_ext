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
    echo " fnmusic-ext 安装配置"
    echo " 音源: ${MUSICDL_REPO}"
    echo "       ${MUSICBOX_REPO}"
    echo "============================================================"
    if [ -z "${MODE}" ]; then
        echo "请选择安装模式:"
        echo "  1) docker  — 音源以容器运行（推荐，与飞牛系统隔离）"
        echo "  2) host    — 音源以宿主机 Python venv 运行"
        local_choice="$(prompt "输入 1 或 2" "1")"
        case "${local_choice}" in
            2|host) MODE="host" ;;
            *) MODE="docker" ;;
        esac
    fi
    if [ -z "${SOURCES_RAW}" ]; then
        echo "请选择音源（可多选，逗号分隔，至少选一个）:"
        echo "  1) musicdl   — CharlesPikachu/musicdl（酷我/咪咕）"
        echo "  2) musicbox  — darknessomi/musicbox（网易云）"
        SOURCES_RAW="$(prompt "输入 1 / 2 / 1,2" "1,2")"
    fi
    if [ -z "${ENABLE_RECOMMEND}" ]; then
        rec_choice="$(prompt "是否开启每日推荐（调用大模型，需 OpenAI 兼容接口）? y/N" "N")"
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
    ext_choice="$(prompt "安装完成后是否立即执行 extend.sh 启用扩展? y/N" "N")"
    case "${ext_choice}" in
        y|Y|yes|YES) RUN_EXTEND=1 ;;
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
log_info "安装配置完成。已启用音源:${SELECTED}"
log_info "下一步（不会改飞牛系统文件）:"
log_info "  1. 确认飞牛音乐应用已启动"
log_info "  2. ./extend.sh     # 一键接管 Unix Socket"
log_info "  3. ./restore.sh    # 一键还原官方直连"
if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    log_info "网易云部分曲目可能需要扫码: http://127.0.0.1:8770/api/v1/auth/login/qr.png"
fi
if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
    log_info "每日推荐已配置。登录飞牛音乐后，歌单列表顶部会出现「每日推荐」。"
else
    log_info "每日推荐未开启。之后可重新运行本脚本填写 LLM 配置。"
fi
log_info "============================================================"

if [ "${RUN_EXTEND}" -eq 1 ]; then
    exec "${BASE_DIR}/extend.sh"
fi
