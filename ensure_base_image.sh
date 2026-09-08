#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext Docker 基础镜像源探测 / 保障（v1.2.3+）
# 背景：fnOS 等 NAS 系统通常在 Docker daemon 全局配置镜像加速器（如 docker.fnnas.com），
#       加速器异常（401/超时）时 BuildKit 解析 python:3.13-slim 元数据会失败且不会
#       回退官方源，导致 docker compose up --build 直接失败。
# 本脚本不修改任何系统配置，仅为本应用解析一个「真实可拉取」的基础镜像引用：
#   1. 环境变量 BASE_IMAGE 手动指定 → 仅验证该引用（跳过自动探测）
#   2. .env 已缓存的镜像源引用 FNMUSIC_BASE_IMAGE → 真实拉取验证，可用即沿用
#   3. 国内镜像优先逐个尝试（环境变量 FNMUSIC_DOCKER_MIRRORS 空格分隔可覆盖）：
#      daemon 加速器（如 docker.fnnas.com）只拦截 docker.io 短引用，完整镜像源引用
#      直连对应仓库，绕开故障/限速的加速器，稳定可靠
#   4. 官方 python:3.13-slim 作为最后兜底（走 daemon 加速器链路，国内网络下常慢/不稳）
# 探测结果经 proxy/env_merge.py 安全增量写入 .env 的 FNMUSIC_BASE_IMAGE（备份+600 权限），
# docker-compose.yml 的 build.args 自动读取该值；install.sh / extend.sh / 手动重建全部生效。
# 用法: bash ensure_base_image.sh   （由 install.sh / extend.sh 在 docker 模式构建前调用）
# 可调环境变量: BASE_IMAGE / FNMUSIC_DOCKER_MIRRORS / PULL_TIMEOUT（单次拉取超时秒数，默认 240）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PATH="${BASE_DIR}/.env"
BASE_TAG="python:3.13-slim"
DEFAULT_MIRRORS="docker.1ms.run docker.m.daocloud.io docker.1panel.live hub.rat.dev"
PULL_TIMEOUT="${PULL_TIMEOUT:-240}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

# 与 install.sh / extend.sh 同款 docker 访问回退（docker 或 sudo docker）
if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
else
    log_err "Docker daemon 不可用（docker / sudo docker 均无法连接），无法探测基础镜像源。"
    exit 1
fi

# 读取 .env 中已缓存的 FNMUSIC_BASE_IMAGE（只取该键，不打印 .env 其他内容）
cached_base_image() {
    [ -f "${ENV_PATH}" ] || return 0
    grep -E "^\s*(export\s+)?FNMUSIC_BASE_IMAGE=" "${ENV_PATH}" 2>/dev/null \
        | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true
}

# 用真实 docker pull 验证（与 BuildKit 构建走同一条 daemon 链路，顺带预取镜像层）
pull_ok() {
    local ref="$1"
    # shellcheck disable=SC2086
    timeout "${PULL_TIMEOUT}" ${DOCKER_CMD} pull "$ref" >/dev/null 2>&1
}

# 安全增量写入 FNMUSIC_BASE_IMAGE（沿用 install.sh 的 env_merge 备份惯例）
persist_base_image() {
    local ref="$1" desired
    desired="$(mktemp)"
    echo "FNMUSIC_BASE_IMAGE='$(printf "%s" "${ref}" | sed "s/'/'\\\\''/g")'" > "${desired}"
    if [ -f "${ENV_PATH}" ]; then
        cp -p "${ENV_PATH}" "${ENV_PATH}.bak.$(date +%Y%m%d%H%M%S)"
        python3 "${BASE_DIR}/proxy/env_merge.py" \
            --existing "${ENV_PATH}" --desired "${desired}" \
            --output "${ENV_PATH}" --explicit FNMUSIC_BASE_IMAGE --quiet
    else
        python3 "${BASE_DIR}/proxy/env_merge.py" \
            --existing /dev/null --desired "${desired}" \
            --output "${ENV_PATH}" --explicit FNMUSIC_BASE_IMAGE --quiet
    fi
    rm -f "${desired}"
    chmod 600 "${ENV_PATH}"
}

MANUAL="${BASE_IMAGE:-}"
CACHED="$(cached_base_image)"
MIRRORS="${FNMUSIC_DOCKER_MIRRORS:-${DEFAULT_MIRRORS}}"

# 组装候选列表（保序去重；手动指定时仅验证该引用）
CANDIDATES=()
SEEN=" "
add_candidate() {
    local c="$1"
    [ -z "${c}" ] && return 0
    case "${SEEN}" in *" ${c} "*) return 0 ;; esac
    SEEN="${SEEN}${c} "
    CANDIDATES+=("${c}")
}
if [ -n "${MANUAL}" ]; then
    log_info "已通过 BASE_IMAGE 手动指定基础镜像，跳过自动探测。"
    add_candidate "${MANUAL}"
else
    # 缓存的镜像源引用优先复用；缓存的官方短引用（docker.io 域）不提前——
    # daemon 加速器链路不稳时它最慢最不可靠，统一沉底做最后兜底
    case "${CACHED}" in
        ""|"${BASE_TAG}"|"docker.io/"*|"registry.hub.docker.com/"*) ;;
        *) add_candidate "${CACHED}" ;;
    esac
    local_mirrors="${MIRRORS}"
    # shellcheck disable=SC2086
    for m in ${local_mirrors}; do
        add_candidate "${m}/library/${BASE_TAG}"
    done
    add_candidate "${BASE_TAG}"
fi

CHOSEN=""
for ref in "${CANDIDATES[@]}"; do
    log_info "尝试拉取基础镜像: ${ref} ..."
    rc=0
    pull_ok "${ref}" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        CHOSEN="${ref}"
        break
    fi
    log_warn "拉取 ${ref} 失败（exit=${rc}），尝试下一个候选..."
done

if [ -z "${CHOSEN}" ]; then
    log_err "所有基础镜像候选均拉取失败: ${CANDIDATES[*]}"
    log_err "可尝试：1) 设置 BASE_IMAGE 环境变量手动指定可用镜像引用（如 docker.m.daocloud.io/library/python:3.13-slim）；"
    log_err "        2) 通过 FNMUSIC_DOCKER_MIRRORS 自定义国内镜像候选列表；"
    log_err "        3) 检查 fnOS Docker 镜像加速器（如 docker.fnnas.com）是否可用，或改用 ./install.sh --mode host。"
    exit 1
fi

if [ "${CHOSEN}" = "${CACHED}" ]; then
    log_info "基础镜像源可用: ${CHOSEN}（沿用 .env 缓存，无需更新）"
else
    persist_base_image "${CHOSEN}"
    log_info "基础镜像源已确定并写入 .env: FNMUSIC_BASE_IMAGE=${CHOSEN}"
fi
