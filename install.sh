#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键安装 / 配置
# - 音源可多选、至少选一个：
#     musicbox https://github.com/darknessomi/musicbox   (:8770 网易云)
#     musicdl  https://github.com/CharlesPikachu/musicdl (:8768 聚合)
#     lxmusic  洛雪音乐源（LX Music 免登录解析）          (:8772)
# - 可选开启每日推荐（OpenAI 兼容接口；不填则关闭）
# - 不修改飞牛 nginx / 官方二进制 / 官方数据库写入
# 用法:
#   ./install.sh                         # 交互
#   ./install.sh --mode host
#   ./install.sh --mode docker --sources=1,2,3
#   ./install.sh --mode docker --sources musicbox,lxmusic
#   ./install.sh --non-interactive --mode docker --enable-recommend \
#       --llm-base-url https://api.example.com/v1 --llm-api-key '***' --llm-model gpt-4o-mini
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FNMUSIC_VERSION="$(head -n 1 "${BASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"
MODE=""
SOURCES_RAW=""
NON_INTERACTIVE=0
ENABLE_RECOMMEND=""
LLM_BASE_URL=""
LLM_API_KEY=""
LLM_MODEL=""
LLM_MODEL_FROM_CLI=0
DEFAULT_LLM_MODEL="gpt-4o-mini"
RUN_EXTEND=0
ENABLE_MUSICDL=0
ENABLE_MUSICBOX=0
ENABLE_LX=0
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
MUSICDL_REPO="${MUSICDL_REPO:-https://github.com/CharlesPikachu/musicdl}"
MUSICBOX_REPO="${MUSICBOX_REPO:-https://github.com/darknessomi/musicbox}"
BASE_IMAGE="${BASE_IMAGE:-}"
DOCKER_IMAGE_MIRRORS="${DOCKER_IMAGE_MIRRORS:-docker.1ms.run docker.m.daocloud.io docker.1panel.live hub.rat.dev}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

usage() {
    cat <<'EOF'
用法: ./install.sh [选项]

  --mode host|docker     安装模式（host=宿主机 venv；docker=音源容器）
  --sources LIST         音源，逗号分隔，可多选，至少选一个（支持 --sources=1,2,3 形式）
                         取值: musicbox, musicdl, lxmusic（或 1, 2, 3）
                         1 = musicbox 网易云 [8770]
                         2 = musicdl  聚合    [8768]
                         3 = lxmusic  洛雪音乐源 [8772]
                         非交互缺省: musicdl
  --non-interactive      无交互，缺省值：mode=docker，音源=musicdl，不开启每日推荐
  --enable-recommend     开启每日推荐（需同时给 base-url 与 api-key）
  --disable-recommend    明确关闭每日推荐
  --llm-base-url URL     OpenAI 兼容 Base URL，例如 https://api.openai.com/v1
  --llm-api-key KEY      API Key（不会回显；请勿提交到 git）
  --llm-model NAME       模型名；交互模式可自动拉取列表选择；非交互缺省 gpt-4o-mini
  --extend               安装完成后立即执行 ./extend.sh
  --qr                   启动终端网易云扫码登录流程
  -h, --help             显示帮助

密钥只写入仓库根目录 .env（chmod 600），不会进入 systemd 文件或日志。
EOF
}

parse_sources() {
    local raw="${1:-}"
    ENABLE_MUSICDL=0
    ENABLE_MUSICBOX=0
    ENABLE_LX=0
    # 支持 --sources=1,2,3 与 --sources 1,2,3 两种形式
    raw="${raw#*=}"
    raw="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | tr ' ' ',')"
    local IFS=','
    local part
    # shellcheck disable=SC2086
    for part in ${raw}; do
        part="${part#"${part%%[![:space:]]*}"}"
        part="${part%"${part##*[![:space:]]}"}"
        [ -z "${part}" ] && continue
        case "${part}" in
            1|musicbox|netease|netease-musicbox) ENABLE_MUSICBOX=1 ;;
            2|musicdl|mdl) ENABLE_MUSICDL=1 ;;
            3|lx|lxmusic) ENABLE_LX=1 ;;
            *)
                log_err "未知音源: ${part}（可选 musicbox / musicdl / lxmusic，或 1 / 2 / 3）"
                exit 1
                ;;
        esac
    done
    if [ "${ENABLE_MUSICDL}" -eq 0 ] && [ "${ENABLE_MUSICBOX}" -eq 0 ] && [ "${ENABLE_LX}" -eq 0 ]; then
        log_err "至少选择一个音源（musicbox / musicdl / lxmusic）"
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
        --mode)
            [ $# -ge 2 ] || { log_err "--mode 需要参数 host|docker"; exit 1; }
            MODE="${2}"; shift 2 ;;
        --mode=*) MODE="${1#*=}"; shift ;;
        --sources)
            [ $# -ge 2 ] || { log_err "--sources 需要音源列表参数"; exit 1; }
            SOURCES_RAW="${2}"; shift 2 ;;
        --sources=*) SOURCES_RAW="${1#*=}"; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --enable-recommend) ENABLE_RECOMMEND="yes"; shift ;;
        --disable-recommend) ENABLE_RECOMMEND="no"; shift ;;
        --llm-base-url)
            [ $# -ge 2 ] || { log_err "--llm-base-url 需要 URL 参数"; exit 1; }
            LLM_BASE_URL="${2}"; shift 2 ;;
        --llm-api-key)
            [ $# -ge 2 ] || { log_err "--llm-api-key 需要 KEY 参数"; exit 1; }
            LLM_API_KEY="${2}"; shift 2 ;;
        --llm-model)
            [ $# -ge 2 ] || { log_err "--llm-model 需要模型名参数"; exit 1; }
            LLM_MODEL="${2}"
            LLM_MODEL_FROM_CLI=1
            shift 2 ;;
        --extend) RUN_EXTEND=1; shift ;;
        --qr)
            bash "${BASE_DIR}/netease_login.sh"
            exit 0
            ;;
        -h|--help) usage; exit 0 ;;
        *) log_err "未知参数: $1"; usage; exit 1 ;;
    esac
done

run_docker() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        sudo docker "$@"
    else
        return 1
    fi
}

# 容器名全局固定（fnmusic-*）；若被其他副本/并发任务的容器占用，移除后由当前目录接管
reclaim_container() {
    local name="$1" owner=""
    if ! run_docker container inspect "${name}" >/dev/null 2>&1; then
        return 0
    fi
    owner="$(run_docker container inspect "${name}" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
    log_info "检测到同名容器 ${name}（来自 ${owner:-未知目录}），移除后由当前目录接管..."
    if ! run_docker rm -f "${name}"; then
        log_err "无法移除同名容器 ${name}，请手动执行: docker rm -f ${name}"
        return 1
    fi
}

precheck_environment() {
    log_info "==> 开始安装环境预检..."
    local precheck_failed=0

    # 0. curl（健康探测 / 验收 / 二维码均依赖）
    if ! command -v curl >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统未检测到 curl。"
        log_err "请先执行：sudo apt-get update && sudo apt-get install -y curl"
        precheck_failed=1
    else
        log_info "curl 已就绪。"
    fi

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
        if run_docker info >/dev/null 2>&1; then
            log_info "Docker 容器环境已就绪。"
        else
            log_warn "检测到 docker 命令，但当前用户无法连通 Docker daemon。"
            if [ "${MODE}" = "docker" ]; then
                log_err "【权限不足】Docker 模式需要可用的 docker（或 sudo docker）。"
                precheck_failed=1
            fi
        fi
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

ensure_docker_ready() {
    if ! command -v docker >/dev/null 2>&1 || ! run_docker info >/dev/null 2>&1; then
        log_err "【缺少组件】已选择 Docker 模式，但 Docker 不可用。"
        log_err "请先在 fnOS 应用中心安装 Docker，或改用 --mode host。"
        exit 1
    fi
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

# 从 OpenAI 兼容接口拉取模型列表（失败返回空；不打印 API Key）
fetch_llm_models() {
    local base_url="$1" api_key="$2"
    local models_url tmp_body http_code
    base_url="${base_url%/}"
    models_url="${base_url}/models"
    tmp_body="$(mktemp)"
    http_code="$(
        curl -sS --max-time 15 \
            -H "Authorization: Bearer ${api_key}" \
            -H "Content-Type: application/json" \
            -o "${tmp_body}" -w "%{http_code}" \
            "${models_url}" 2>/dev/null || echo "000"
    )"
    if [ "${http_code}" != "200" ]; then
        rm -f "${tmp_body}"
        return 1
    fi
    if ! python3 - "${tmp_body}" <<'PY' 2>/dev/null
import json, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
rows = []
if isinstance(data, dict):
    raw = data.get("data")
    if isinstance(raw, list):
        rows = raw
    elif isinstance(data.get("models"), list):
        rows = data["models"]
elif isinstance(data, list):
    rows = data
ids = []
seen = set()
for it in rows:
    mid = ""
    if isinstance(it, dict):
        mid = str(it.get("id") or it.get("name") or it.get("model") or "").strip()
    elif isinstance(it, str):
        mid = it.strip()
    if mid and mid not in seen:
        seen.add(mid)
        ids.append(mid)
if not ids:
    sys.exit(1)
for mid in ids:
    print(mid)
PY
    then
        rm -f "${tmp_body}"
        return 1
    fi
    rm -f "${tmp_body}"
    return 0
}

# 交互选择模型：优先展示拉取到的列表，失败则手写
prompt_llm_model() {
    local base_url="$1" api_key="$2"
    local models=() line i choice custom def_idx=1
    log_info "正在从接口拉取可用模型列表..."
    while IFS= read -r line; do
        [ -n "${line}" ] && models+=("${line}")
    done < <(fetch_llm_models "${base_url}" "${api_key}" || true)

    if [ "${#models[@]}" -eq 0 ]; then
        log_warn "未能自动获取模型列表（接口不可达、鉴权失败或返回格式不兼容）。"
        LLM_MODEL="$(prompt "请手动输入模型名称" "${DEFAULT_LLM_MODEL}")"
        LLM_MODEL="${LLM_MODEL:-${DEFAULT_LLM_MODEL}}"
        return 0
    fi

    local max_show=40 total="${#models[@]}"
    if [ "${total}" -gt "${max_show}" ]; then
        log_info "接口返回 ${total} 个模型，列表仅展示前 ${max_show} 个；其余请选 0 自定义输入。"
    fi
    echo "可用模型："
    local show_count="${total}"
    [ "${show_count}" -gt "${max_show}" ] && show_count="${max_show}"
    for i in $(seq 0 $((show_count - 1))); do
        echo "  $((i + 1))) ${models[$i]}"
    done
    echo "  0) 自定义输入模型名称"
    # 默认选第一项；若可见列表含默认模型名则优先
    for i in $(seq 0 $((show_count - 1))); do
        if [ "${models[$i]}" = "${DEFAULT_LLM_MODEL}" ]; then
            def_idx=$((i + 1))
            break
        fi
    done
    choice="$(prompt "请选择模型编号（0=自定义）" "${def_idx}")"
    case "${choice}" in
        0)
            custom="$(prompt "请输入自定义模型名称" "${DEFAULT_LLM_MODEL}")"
            LLM_MODEL="${custom:-${DEFAULT_LLM_MODEL}}"
            ;;
        ''|*[!0-9]*)
            log_warn "输入无效，使用默认模型 ${models[$((def_idx - 1))]}。"
            LLM_MODEL="${models[$((def_idx - 1))]}"
            ;;
        *)
            if [ "${choice}" -ge 1 ] && [ "${choice}" -le "${show_count}" ]; then
                LLM_MODEL="${models[$((choice - 1))]}"
            else
                log_warn "编号超出范围，使用默认模型 ${models[$((def_idx - 1))]}。"
                LLM_MODEL="${models[$((def_idx - 1))]}"
            fi
            ;;
    esac
    log_info "已选择模型: ${LLM_MODEL}"
}

if [ "${NON_INTERACTIVE}" -eq 0 ]; then
    echo "============================================================"
    echo " fnmusic-ext 安装配置向导  v${FNMUSIC_VERSION}"
    echo " 音源: ${MUSICBOX_REPO}"
    echo "       ${MUSICDL_REPO}"
    echo "       lxmusic — 洛雪音乐源（免登录解析：酷狗/网易/咪咕）"
    echo "============================================================"
    if [ -z "${MODE}" ]; then
        echo "【安装模式说明】"
        echo "  无论选哪种模式，核心代理（fnmusic-ext）均以宿主机 systemd 运行接管 Socket。"
        echo "  两种模式区别仅在于音源服务（musicbox/musicdl/lxmusic）的部署运行形态："
        if command -v docker >/dev/null 2>&1; then
            echo "  1) docker  — [推荐] Docker 容器模式："
            echo "               通过 compose 运行轻量容器（端口 8768/8770/8772，无特权，数据隔离在 musicbox-data/）"
            echo "  2) host    — Host 宿主机本地服务模式（纯净无 Docker）："
            echo "               创建独立 Python venv 并注册为 systemd 服务（监听 127.0.0.1，不污染全局环境）"
            local_choice="$(prompt "请选择安装模式 (输入 1 或 2)" "1")"
        else
            echo "  1) docker  — Docker 容器模式（未检测到 Docker，若选此项请先在 fnOS「应用中心」安装 Docker）"
            echo "  2) host    — [推荐当前环境] Host 宿主机本地服务模式："
            echo "               纯净无 Docker，通过项目内独立 Python venv 运行并注册为 systemd 服务"
            local_choice="$(prompt "请选择安装模式 (输入 1 或 2)" "2")"
        fi
        case "${local_choice}" in
            2|host) MODE="host" ;;
            *) MODE="docker" ;;
        esac
    fi
    if [ -z "${SOURCES_RAW}" ]; then
        echo "请选择音源（可多选，逗号分隔，至少选一个）:"
        echo "  1) 网易云音乐源 musicbox  [端口 8770] — 高品质/无损/歌词封面（darknessomi/musicbox）"
        echo "  2) 聚合音源   musicdl    [端口 8768] — 酷我/咪咕等聚合，覆盖热门流行（CharlesPikachu/musicdl）"
        echo "  3) 洛雪音乐源 lxmusic     [端口 8772] — 免登录高音质解析：酷狗 kg / 网易 wy / 咪咕 mg 直链"
        echo "  1,2,3) 全部启用 — 三源并行（推荐）"
        SOURCES_RAW="$(prompt "输入 1 / 2 / 3 / 1,2 / 1,3 / 1,2,3" "1,2,3")"
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
        if [ -z "${LLM_BASE_URL}" ] || [ -z "${LLM_API_KEY}" ]; then
            log_warn "未同时提供 Base URL 与 API Key，每日推荐将关闭。"
            ENABLE_RECOMMEND="no"
            LLM_BASE_URL=""
            LLM_API_KEY=""
            LLM_MODEL=""
        elif [ "${LLM_MODEL_FROM_CLI}" -eq 1 ] && [ -n "${LLM_MODEL}" ]; then
            log_info "使用命令行指定的模型: ${LLM_MODEL}"
        else
            # 仅在开启推荐且已有 URL/Key 时拉取模型列表并让用户选择
            prompt_llm_model "${LLM_BASE_URL}" "${LLM_API_KEY}"
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
        LLM_MODEL="${LLM_MODEL:-${DEFAULT_LLM_MODEL}}"
    else
        ENABLE_RECOMMEND="no"
        LLM_BASE_URL=""
        LLM_API_KEY=""
        LLM_MODEL=""
    fi
fi

MODE="${MODE:-docker}"
if [ "${MODE}" != "host" ] && [ "${MODE}" != "docker" ]; then
    log_err "mode 必须是 host 或 docker"
    exit 1
fi
if [ "${MODE}" = "docker" ]; then
    ensure_docker_ready
fi

parse_sources "${SOURCES_RAW}"

SELECTED=""
[ "${ENABLE_MUSICBOX}" -eq 1 ] && SELECTED="${SELECTED} musicbox[8770]"
[ "${ENABLE_MUSICDL}" -eq 1 ] && SELECTED="${SELECTED} musicdl[8768]"
[ "${ENABLE_LX}" -eq 1 ] && SELECTED="${SELECTED} lxmusic[8772]"

log_info "fnmusic-ext v${FNMUSIC_VERSION}"
log_info "安装模式: ${MODE}"
log_info "音源:${SELECTED}"
log_info "每日推荐: ${ENABLE_RECOMMEND}"
log_info "项目目录: ${BASE_DIR}"

mkdir -p "${BASE_DIR}/cache" "${BASE_DIR}/online_favorites" "${BASE_DIR}/play_history" "${BASE_DIR}/recommend_cache" \
    "${BASE_DIR}/musicbox-data/cache/netease-musicbox" \
    "${BASE_DIR}/musicbox-data/config/netease-musicbox" \
    "${BASE_DIR}/musicbox-data/netease-musicbox"
chmod -R 777 "${BASE_DIR}/musicbox-data" 2>/dev/null || true

# 归一化服务源码权限：umask 077 环境检出的文件为 600，会导致镜像内 appuser 读不到 app.py
chmod 0644 \
    "${BASE_DIR}/musicdl-service/app.py" "${BASE_DIR}/musicdl-service/hardening.py" \
    "${BASE_DIR}/musicbox-service/app.py" "${BASE_DIR}/musicbox-service/runner.py" \
    "${BASE_DIR}/musicbox-service/netease_ext.py" "${BASE_DIR}/lxmusic-service/app.py" \
    2>/dev/null || true

MUSICDL_FLAG="false"
MUSICBOX_FLAG="false"
LX_FLAG="false"
[ "${ENABLE_MUSICDL}" -eq 1 ] && MUSICDL_FLAG="true"
[ "${ENABLE_MUSICBOX}" -eq 1 ] && MUSICBOX_FLAG="true"
[ "${ENABLE_LX}" -eq 1 ] && LX_FLAG="true"

# --- 写 .env（防覆盖：安全增量合并，脱敏：不打印 key） ---
ENV_PATH="${BASE_DIR}/.env"
umask 077
ENV_DESIRED="$(mktemp)"
{
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
    echo "FNMUSIC_LX_ENABLED='${LX_FLAG}'"
    echo "FNMUSIC_LX_URL='http://127.0.0.1:8772'"
    echo "FNMUSIC_DEPLOY_MODE='${MODE}'"
    echo "FNMUSIC_PIP_INDEX='$(dotenv_escape "${PIP_INDEX}")'"
    if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
        echo "FNMUSIC_LLM_BASE_URL='$(dotenv_escape "${LLM_BASE_URL}")'"
        echo "FNMUSIC_LLM_API_KEY='$(dotenv_escape "${LLM_API_KEY}")'"
        echo "FNMUSIC_LLM_MODEL='$(dotenv_escape "${LLM_MODEL}")'"
    else
        echo "FNMUSIC_LLM_BASE_URL=''"
        echo "FNMUSIC_LLM_API_KEY=''"
        echo "FNMUSIC_LLM_MODEL=''"
    fi
    echo "FNMUSIC_VERSION='${FNMUSIC_VERSION}'"
} > "${ENV_DESIRED}"

# 用户本次明确提供了新值的键（音源开关/版本/部署模式为安装时部署选项，始终采用新值）
ENV_EXPLICIT="FNMUSIC_MUSICDL_ENABLED,FNMUSIC_NETEASE_ENABLED,FNMUSIC_LX_ENABLED,FNMUSIC_VERSION,FNMUSIC_DEPLOY_MODE"
[ "${ENABLE_LX}" -eq 1 ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LX_URL"
if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
    [ -n "${LLM_BASE_URL}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL"
    [ -n "${LLM_API_KEY}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_API_KEY"
    [ -n "${LLM_MODEL}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_MODEL"
else
    # 关闭推荐时必须显式覆盖，否则 env_merge 会保留旧 Key
    ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL,FNMUSIC_LLM_API_KEY,FNMUSIC_LLM_MODEL"
fi

if [ -f "${ENV_PATH}" ]; then
    PREV_VERSION="$(grep -E "^\s*(export\s+)?FNMUSIC_VERSION=" "${ENV_PATH}" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true)"
    PREV_VERSION="${PREV_VERSION:-}"
    ENV_BACKUP="${ENV_PATH}.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "${ENV_PATH}" "${ENV_BACKUP}"
    if [ -n "${PREV_VERSION}" ] && [ "${PREV_VERSION}" = "${FNMUSIC_VERSION}" ]; then
        log_warn "检测到同版本 (v${FNMUSIC_VERSION}) 重复安装：现有配置将被保护，"
        log_warn "仅补齐缺失配置项；密钥/自定义路径/ONLINE_SOURCES 等沿用已有值（备份: ${ENV_BACKUP}）。"
    else
        log_info "检测到已有配置（v${PREV_VERSION:-未知} -> v${FNMUSIC_VERSION}）平滑升级："
        log_info "保留用户自定义配置与密钥，仅安全补齐新增/缺失配置项（备份: ${ENV_BACKUP}）。"
    fi
    MERGE_SUMMARY="$(python3 "${BASE_DIR}/proxy/env_merge.py" \
        --existing "${ENV_PATH}" --desired "${ENV_DESIRED}" \
        --output "${ENV_PATH}" --explicit "${ENV_EXPLICIT}" 2>&1)" || {
        log_err "配置合并失败，已保留原配置不动: ${ENV_PATH}"
        rm -f "${ENV_DESIRED}"
        exit 1
    }
    log_info "配置合并完成 (v${FNMUSIC_VERSION})："
    while IFS= read -r line; do
        [ -n "${line}" ] && log_info "  ${line}"
    done <<< "${MERGE_SUMMARY}"
else
    python3 "${BASE_DIR}/proxy/env_merge.py" \
        --existing /dev/null --desired "${ENV_DESIRED}" \
        --output "${ENV_PATH}" --explicit "${ENV_EXPLICIT}" --quiet
    log_info "已生成初始配置 ${ENV_PATH} (chmod 600)。API Key 不会出现在日志中。"
fi
rm -f "${ENV_DESIRED}"
chmod 600 "${ENV_PATH}"

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
"${BASE_DIR}/.venv-proxy/bin/pip" install -q -r "${BASE_DIR}/proxy/requirements.txt" -i "${PIP_INDEX}"

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
    reclaim_container fnmusic-musicdl || return 1
    run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build musicdl
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
    mkdir -p "${BASE_DIR}/musicbox-data/cache/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/config/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/netease-musicbox"
    chmod -R 777 "${BASE_DIR}/musicbox-data" 2>/dev/null || true
    log_info "构建并启动 musicbox 容器（基于 ${MUSICBOX_REPO}）..."
    reclaim_container fnmusic-musicbox || return 1
    run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build musicbox
    if wait_http "http://127.0.0.1:8770/healthz" 60 2; then
        log_info "musicbox 已就绪 http://127.0.0.1:8770/healthz"
        return 0
    fi
    log_err "等待 musicbox healthz 超时"
    return 1
}

install_musicbox_host() {
    log_info "宿主机安装 musicbox 服务（pip 包来自 ${MUSICBOX_REPO}）..."
    mkdir -p "${BASE_DIR}/musicbox-data/cache/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/config/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/netease-musicbox"
    chmod -R 777 "${BASE_DIR}/musicbox-data" 2>/dev/null || true
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
ExecStart=${BASE_DIR}/.venv-musicbox/bin/uvicorn app:app --host 0.0.0.0 --port 8770
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

# --- lxmusic（洛雪音乐源） ---
install_lxmusic_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        log_err "未找到 docker，无法使用 docker 模式。请安装 Docker 或改用 --mode host"
        return 1
    fi
    log_info "构建并启动 lxmusic 容器（洛雪音乐源：酷狗 kg / 网易 wy / 咪咕 mg 免登录解析）..."
    reclaim_container fnmusic-lxmusic || return 1
    run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build lxmusic
    if wait_http "http://127.0.0.1:8772/healthz" 60 2; then
        log_info "lxmusic 已就绪 http://127.0.0.1:8772/healthz"
        return 0
    fi
    log_err "等待 lxmusic healthz 超时"
    return 1
}

install_lxmusic_host() {
    log_info "宿主机安装 lxmusic 服务（洛雪音乐源）..."
    if [ ! -x "${BASE_DIR}/.venv-lxmusic/bin/python" ]; then
        python3 -m venv "${BASE_DIR}/.venv-lxmusic"
    fi
    "${BASE_DIR}/.venv-lxmusic/bin/pip" install -q -U pip -i "${PIP_INDEX}"
    "${BASE_DIR}/.venv-lxmusic/bin/pip" install -q -r "${BASE_DIR}/lxmusic-service/requirements.txt" -i "${PIP_INDEX}"
    local unit
    unit="$(mktemp)"
    cat > "${unit}" <<EOF
[Unit]
Description=fnmusic-ext lxmusic source (LX Music style: kg/wy/mg)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${BASE_DIR}/lxmusic-service
Environment=PYTHONUNBUFFERED=1
Environment=LX_SOURCES=kg,wy,mg
ExecStart=${BASE_DIR}/.venv-lxmusic/bin/uvicorn app:app --host 127.0.0.1 --port 8772
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    if ! install_unit "${unit}" /etc/systemd/system/fnmusic-lxmusic.service; then
        return 0
    fi
    if wait_http "http://127.0.0.1:8772/healthz" 30 1; then
        log_info "宿主机 lxmusic 已就绪"
        return 0
    fi
    log_warn "lxmusic systemd 已启动，但 healthz 尚未就绪，请检查 journalctl -u fnmusic-lxmusic"
}

clear_opposite_mode() {
    # 交叉模式切换时清理对侧，避免端口占用冲突
    if [ "${MODE}" = "docker" ]; then
        log_info "Docker 模式：停用宿主机音源 systemd unit（若存在）..."
        for unit in fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic; do
            sudo systemctl disable --now "${unit}.service" 2>/dev/null || true
        done
    else
        log_info "Host 模式：停止 Docker 音源容器（若存在）..."
        run_docker rm -f fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic 2>/dev/null || true
    fi
}

stop_unselected() {
    if [ "${ENABLE_MUSICDL}" -eq 0 ]; then
        run_docker rm -f fnmusic-musicdl 2>/dev/null || true
        sudo systemctl disable --now fnmusic-musicdl.service 2>/dev/null || true
    fi
    if [ "${ENABLE_MUSICBOX}" -eq 0 ]; then
        run_docker rm -f fnmusic-musicbox 2>/dev/null || true
        sudo systemctl disable --now fnmusic-musicbox.service 2>/dev/null || true
    fi
    if [ "${ENABLE_LX}" -eq 0 ]; then
        run_docker rm -f fnmusic-lxmusic 2>/dev/null || true
        sudo systemctl disable --now fnmusic-lxmusic.service 2>/dev/null || true
    fi
}

# Docker 模式：先探测可用基础镜像源（国内镜像优先直连、官方源兜底），
# 结果写入 .env 的 FNMUSIC_BASE_IMAGE 供 compose build.args 使用；失败直接退出，不动现有部署
if [ "${MODE}" = "docker" ]; then
    if ! BASE_IMAGE="${BASE_IMAGE}" FNMUSIC_DOCKER_MIRRORS="${DOCKER_IMAGE_MIRRORS}" \
        bash "${BASE_DIR}/ensure_base_image.sh"; then
        log_err "基础镜像源探测失败。可设置 BASE_IMAGE 环境变量手动指定可用镜像源，或改用 --mode host。"
        exit 1
    fi
fi

clear_opposite_mode
if [ "${MODE}" = "docker" ]; then
    [ "${ENABLE_MUSICDL}" -eq 1 ] && install_musicdl_docker
    [ "${ENABLE_MUSICBOX}" -eq 1 ] && install_musicbox_docker
    [ "${ENABLE_LX}" -eq 1 ] && install_lxmusic_docker
else
    [ "${ENABLE_MUSICDL}" -eq 1 ] && install_musicdl_host
    [ "${ENABLE_MUSICBOX}" -eq 1 ] && install_musicbox_host
    [ "${ENABLE_LX}" -eq 1 ] && install_lxmusic_host
fi
stop_unselected

python3 -m py_compile "${BASE_DIR}/proxy/app.py" "${BASE_DIR}/proxy/recommend.py"
bash -n "${BASE_DIR}/extend.sh" "${BASE_DIR}/restore.sh" "${BASE_DIR}/proxy/run_proxy.sh" "${BASE_DIR}/netease_login.sh" "${BASE_DIR}/ensure_base_image.sh"

log_info "============================================================"
log_info "🎉 fnmusic-ext v${FNMUSIC_VERSION} 安装配置完成！"
log_info "已启用音源（安装模式: ${MODE}）:${SELECTED}"
log_info "------------------------------------------------------------"
log_info "【音源服务状态】"
[ "${ENABLE_MUSICBOX}" -eq 1 ] && log_info "  • musicbox  [8770] 网易云音源     http://127.0.0.1:8770/healthz"
[ "${ENABLE_MUSICDL}" -eq 1 ] && log_info "  • musicdl   [8768] 聚合音源      http://127.0.0.1:8768/healthz"
[ "${ENABLE_LX}" -eq 1 ] && log_info "  • lxmusic   [8772] 洛雪音乐源    http://127.0.0.1:8772/healthz"
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
    log_info "   部分网易云 VIP/无损歌曲需要账号凭证："
    log_info "   • 命令行扫码登录（推荐）: ./install.sh --qr 或 ./netease_login.sh"
    log_info "     （自动展示二维码、轮询登录状态、过期自动刷新，支持随时 Ctrl+C 跳过）"
    log_info "   • 局域网浏览器图片（备选）: http://<NAS_IP>:8770/api/v1/auth/login/qr.png"
    log_info "   • 检查登录状态: curl -s http://127.0.0.1:8770/api/v1/auth/status"
fi
if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
    log_info "4. 大模型每日推荐："
    log_info "   已成功配置大模型！登录飞牛音乐后，左侧歌单列表顶部会自动出现「每日推荐」。"
fi
log_info "5. 状态探测与一键还原："
log_info "   • 探测健康状态: curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz"
log_info "   • 随时一键还原: ./restore.sh (立即恢复官方出厂直连状态)"
log_info "============================================================"

if [ "${NON_INTERACTIVE}" -eq 0 ] && [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    log_info ""
    log_info "==> 检测到已启用网易云音源 (musicbox)，即将进入扫码登录流程..."
    bash "${BASE_DIR}/netease_login.sh" || true
fi

if [ "${RUN_EXTEND}" -eq 1 ]; then
    exec "${BASE_DIR}/extend.sh" --force
fi
