#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 原生（无 Docker）音源运行时保障
# 由 install.sh / extend.sh 在 FNMUSIC_DEPLOY_MODE=native 时调用，也可手动重跑。
# 对齐 container/Dockerfile 的镜像层职责，全部落在宿主机：
#   1. 选择合格宿主机 Python（显式配置 → fnOS python312 → 系统），确保
#      .venv-sources 存在；依赖未变化且校验通过时跳过联网安装
#      （容器内由镜像 pip 层安装；多 pip 源回退链与 ensure_proxy_deps.sh 同策略）
#   2. 检测 nodejs（lxserver Node 沙箱）/ ffmpeg（musicdl 无损探针），缺失时
#      尝试 sudo apt-get 安装，失败给出手动命令
#   3. lxserver 预编译包落位 .lxserver/：container/lxserver-artifact/ 预置优先
#      （离线/fpk 场景），缺失走镜像加速下载链；sha256 校验 + 补丁与 Dockerfile
#      构建层一致，按版本幂等（同版本已就位则跳过）；ARM64 额外补齐锁定
#      的 music-tag-native 平台绑定，避免上游 x64 单架构包启动崩溃
# 本脚本不修改任何系统配置（apt 安装除外，且仅补缺失的两组件）。
# 可调环境变量: PIP_INDEX / FNMUSIC_VENV_SOURCES_DIR / FNMUSIC_APT_MIRROR /
#               FNMUSIC_LXSERVER_MIRROR（均默认与 Docker 构建一致）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${BASE_DIR}/proxy/runtime_common.sh" ]; then
    source "${BASE_DIR}/proxy/runtime_common.sh"
else
    runtime_python_ok() { [ -x "$1" ]; }
    runtime_python_version() { "$1" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo unknown; }
    choose_python_bin() {
        if [ -n "${FNMUSIC_PYTHON_BIN:-}" ] && [ -x "${FNMUSIC_PYTHON_BIN}" ]; then
            FNMUSIC_SELECTED_PYTHON="${FNMUSIC_PYTHON_BIN}"
            FNMUSIC_PYTHON_SOURCE="explicit"
            return 0
        fi
        if command -v python3 >/dev/null 2>&1; then
            FNMUSIC_SELECTED_PYTHON="$(command -v python3)"
            FNMUSIC_PYTHON_SOURCE="system python3"
            return 0
        fi
        return 1
    }
    choose_node_bin() {
        if [ -n "${FNMUSIC_NODE_BIN:-}" ] && [ -x "${FNMUSIC_NODE_BIN}" ]; then
            FNMUSIC_SELECTED_NODE="${FNMUSIC_NODE_BIN}"
            FNMUSIC_NODE_SOURCE="explicit"
            return 0
        fi
        if command -v node >/dev/null 2>&1; then
            FNMUSIC_SELECTED_NODE="$(command -v node)"
            FNMUSIC_NODE_SOURCE="system node"
            return 0
        fi
        return 1
    }
    choose_media_bins() {
        FNMUSIC_SELECTED_FFMPEG="$(command -v ffmpeg 2>/dev/null || true)"
        FNMUSIC_SELECTED_FFPROBE="$(command -v ffprobe 2>/dev/null || true)"
        [ -n "${FNMUSIC_SELECTED_FFMPEG}" ]
    }
    deps_requirements_digest() { sha256sum "$@" 2>/dev/null | awk '{print $1}' | sha256sum | awk '{print $1}'; }
    deps_state_valid() { return 1; }
    deps_write_state() { :; }
    deps_imports_ok() { return 0; }
fi
VENV_DIR="${FNMUSIC_VENV_SOURCES_DIR:-${BASE_DIR}/.venv-sources}"
LXSERVER_DIR="${FNMUSIC_LXSERVER_DIR:-${BASE_DIR}/.lxserver}"
LXSERVER_VERSION_FILE="${BASE_DIR}/container/lxserver.version"
LXSERVER_ARTIFACT_DIR="${BASE_DIR}/container/lxserver-artifact"
PIP_INDEX="${PIP_INDEX:-https://mirrors.tencent.com/pypi/simple/}"
FNMUSIC_DEPS_REVISION="sources-20261019-1"
FALLBACK_INDEXES="https://mirrors.aliyun.com/pypi/simple/ https://pypi.tuna.tsinghua.edu.cn/simple https://pypi.org/simple"
APT_MIRROR="${FNMUSIC_APT_MIRROR:-}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

# ---------------------------------------------------------------- 1. venv + pip
REQ_FILES=(
    "${BASE_DIR}/musicdl-service/requirements.txt"
    "${BASE_DIR}/musicbox-service/requirements.txt"
    "${BASE_DIR}/lxmusic-service/requirements.txt"
    "${BASE_DIR}/webui-service/requirements.txt"
)
for req in "${REQ_FILES[@]}"; do
    if [ ! -f "${req}" ]; then
        log_err "缺少依赖清单: ${req}"
        exit 1
    fi
done

if ! choose_python_bin; then
    log_err "未找到可用的 Python 3.11+（需包含 venv/ensurepip/ssl/sqlite3）。"
    log_err "可在飞牛应用中心安装 python312，或设置 FNMUSIC_PYTHON_BIN 指向合格解释器。"
    exit 1
fi
PYTHON_BIN="${FNMUSIC_SELECTED_PYTHON}"
log_info "选用 Python: ${PYTHON_BIN} ($(runtime_python_version "${PYTHON_BIN}")，来源: ${FNMUSIC_PYTHON_SOURCE})"

if [ ! -x "${VENV_DIR}/bin/python" ]; then
    log_info "创建虚拟环境 ${VENV_DIR} ..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
VENV_PYTHON="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"
if [ ! -x "${VENV_PYTHON}" ] || [ ! -x "${PIP_BIN}" ]; then
    log_err "虚拟环境异常：${VENV_PYTHON} 或 ${PIP_BIN} 不存在。"
    log_err "可删除 ${VENV_DIR} 后重跑本脚本重建。"
    exit 1
fi
if ! runtime_python_ok "${VENV_PYTHON}"; then
    log_err "虚拟环境 Python 不满足 3.11+ 或缺少 ensurepip/ssl/sqlite3。"
    log_err "请删除 ${VENV_DIR} 后重跑本脚本重建。"
    exit 1
fi

REQ_DIGEST="$(deps_requirements_digest "${REQ_FILES[@]}")"
if [ "${FNMUSIC_REFRESH_DEPS:-0}" != "1" ] \
   && deps_state_valid "${VENV_DIR}" "${FNMUSIC_DEPS_REVISION}" "${VENV_PYTHON}" \
        "${REQ_DIGEST}" supervisor musicdl fastapi uvicorn httpx curl_cffi PIL qrcode multipart; then
    log_info "音源服务 Python 依赖已就绪，跳过联网安装。"
    SKIP_PIP_INSTALL=1
fi

# supervisor 经 pip 安装（容器内为 apt supervisor；pip 包即同一项目，含
# supervisord/supervisorctl），免去一个系统依赖；supervisorctl -c 指向渲染后的配置。
candidates="${PIP_INDEX}"
for idx in ${FALLBACK_INDEXES}; do
    [ "${idx}" = "${PIP_INDEX}" ] || candidates="${candidates} ${idx}"
done

REQ_ARGS=()
for req in "${REQ_FILES[@]}"; do
    REQ_ARGS+=("-r" "${req}")
done
if [ "${SKIP_PIP_INSTALL:-0}" != "1" ]; then
    rm -f "${VENV_DIR}/.fnmusic-deps"
    ok_index=""
    for idx in ${candidates}; do
        log_info "尝试 pip 源: ${idx}"
        if "${PIP_BIN}" install -q -U pip --retries 2 --timeout 30 -i "${idx}" \
           && "${PIP_BIN}" install -q supervisor "${REQ_ARGS[@]}" --retries 2 --timeout 30 -i "${idx}"; then
            ok_index="${idx}"
            break
        fi
        log_warn "pip 源不可用: ${idx}，切换下一候选源重试..."
    done
    if [ -z "${ok_index}" ]; then
        log_err "所有 pip 源均安装失败（尝试了: ${candidates}）。"
        log_err "排查建议：1) 检查宿主机网络 / DNS / 代理（含 http_proxy、https_proxy 环境变量）；"
        log_err "           2) 换源重试: PIP_INDEX=<其他源URL> bash $(basename "${BASH_SOURCE[0]}")。"
        exit 1
    fi
    if ! "${PIP_BIN}" check >/dev/null 2>&1 \
       || ! deps_imports_ok "${VENV_PYTHON}" supervisor musicdl fastapi uvicorn httpx curl_cffi PIL qrcode multipart; then
        log_err "依赖安装后校验失败（pip check 或关键模块导入失败）。"
        exit 1
    fi
    deps_write_state "${VENV_DIR}" "${FNMUSIC_DEPS_REVISION}" "${VENV_PYTHON}" "${REQ_DIGEST}"
    log_info "音源服务 Python 依赖安装完成（pip 源: ${ok_index}）。"
fi

# ------------------------------------------------------- 2. nodejs / ffmpeg
apt_install_missing() {
    # $@ = 系统包名列表；返回仍缺失的命令数
    local missing=() pkg cmd
    for pkg in "$@"; do
        if ! command -v "${pkg}" >/dev/null 2>&1; then
            missing+=("${pkg}")
        fi
    done
    [ "${#missing[@]}" -eq 0 ] && return 0
    log_warn "检测到缺失系统组件: ${missing[*]}，尝试 sudo apt-get 安装..."
    if command -v apt-get >/dev/null 2>&1 && sudo apt-get update -o Acquire::Retries=1 2>/dev/null \
        && sudo apt-get install -y --no-install-recommends "${missing[@]}"; then
        return 0
    fi
    log_err "apt 自动安装失败，请手动安装后重跑："
    log_err "  sudo apt-get update && sudo apt-get install -y ${missing[*]}"
    return 1
}

apt_install_packages() {
    log_warn "尝试 sudo apt-get 安装/补齐: $*"
    if command -v apt-get >/dev/null 2>&1 && sudo apt-get update -o Acquire::Retries=1 2>/dev/null \
        && sudo apt-get install -y --no-install-recommends "$@"; then
        return 0
    fi
    log_err "apt 自动安装失败，请手动安装后重跑："
    log_err "  sudo apt-get update && sudo apt-get install -y $*"
    return 1
}

if ! choose_node_bin; then
    if [ -n "${FNMUSIC_NODE_BIN:-}" ]; then
        log_err "FNMUSIC_NODE_BIN 不是可执行的 Node 20+：${FNMUSIC_NODE_BIN}"
        exit 1
    fi
    log_warn "未探测到 Node 20+，尝试 sudo apt-get 安装..."
    apt_install_packages nodejs || true
    if ! choose_node_bin; then
        log_err "缺少可用的 Node 20+：lxserver 无法运行。"
        log_err "可在飞牛应用中心安装 nodejs_v22，或设置 FNMUSIC_NODE_BIN 指向 Node 20+。"
        exit 1
    fi
fi
NODE_BIN="${FNMUSIC_SELECTED_NODE}"
log_info "选用 Node: ${NODE_BIN} ($("${NODE_BIN}" --version 2>/dev/null || echo '?')，来源: ${FNMUSIC_NODE_SOURCE})"

choose_media_bins
if [ -z "${FNMUSIC_SELECTED_FFMPEG}" ] || [ -z "${FNMUSIC_SELECTED_FFPROBE}" ]; then
    log_warn "未检测到完整 ffmpeg/ffprobe，尝试 sudo apt-get 补装..."
    apt_install_packages ffmpeg || true
    choose_media_bins
fi
if [ -z "${FNMUSIC_SELECTED_FFMPEG}" ]; then
    log_warn "缺少 ffmpeg：音频解码校验与标准音质转码不可用，将继续安装并按原文件/无法校验处理。"
else
    if [ -z "${FNMUSIC_SELECTED_FFPROBE}" ]; then
        log_warn "检测到 ffmpeg 但缺少 ffprobe：转码完整性复核将降级。"
    fi
    log_info "选用 ffmpeg: ${FNMUSIC_SELECTED_FFMPEG}$( [ -n "${FNMUSIC_SELECTED_FFPROBE}" ] && printf '；ffprobe: %s' "${FNMUSIC_SELECTED_FFPROBE}" )"
fi

# ------------------------------------------------------- 3. lxserver 落位
# 版本锁定文件与 Dockerfile 构建层同源（scripts/update_lxserver.sh 维护）
if [ ! -f "${LXSERVER_VERSION_FILE}" ]; then
    log_err "缺少 ${LXSERVER_VERSION_FILE}，无法确定 lxserver 版本。"
    exit 1
fi
# shellcheck disable=SC1090
. "${LXSERVER_VERSION_FILE}"
LXSERVER_STAMP="${LXSERVER_DIR}/.provisioned-version"
if [ -f "${LXSERVER_DIR}/index.js" ] \
    && [ "$(cat "${LXSERVER_STAMP}" 2>/dev/null || true)" = "${LXSERVER_TAG}" ]; then
    log_info "lxserver ${LXSERVER_TAG} 已就位（${LXSERVER_DIR}），跳过。"
else
    TARGET_ZIP="$(mktemp "${TMPDIR:-/tmp}/lxserver-XXXXXXXX")"
    trap 'rm -f "${TARGET_ZIP}" "${TARGET_ZIP}.tmp" 2>/dev/null || true' EXIT
    PREFETCH="${LXSERVER_ARTIFACT_DIR}/${LXSERVER_ZIP_NAME}"
    DOWNLOAD_OK=0
    if [ -s "${PREFETCH}" ]; then
        log_info "使用预置 lxserver 包: ${PREFETCH}"
        cp -f "${PREFETCH}" "${TARGET_ZIP}"
        DOWNLOAD_OK=1
    else
        # 镜像加速链与 Dockerfile 构建层同策略：国内环境（配置了 apt 镜像）优先走加速器，
        # 否则官方直连优先；全部失败才报错（可预先放置包到 container/lxserver-artifact/）。
        if [ -n "${APT_MIRROR}" ] && [ -z "${FNMUSIC_LXSERVER_MIRROR:-}" ]; then
            _chain="https://ghfast.top https://gh-proxy.com https://github.moeyy.xyz DIRECT"
        else
            _chain="${FNMUSIC_LXSERVER_MIRROR:-https://ghfast.top} https://gh-proxy.com DIRECT"
        fi
        _base="https://github.com/XCQ0607/lxserver/releases/download/${LXSERVER_TAG}/${LXSERVER_ZIP_NAME}"
        for mirror in ${_chain}; do
            if [ "${mirror}" = "DIRECT" ]; then
                URL="${_base}"
            else
                URL="${mirror}/${_base}"
            fi
            log_info "尝试从 ${URL} 下载 lxserver ${LXSERVER_TAG}..."
            if curl -fSL --connect-timeout 10 --max-time 300 "${URL}" -o "${TARGET_ZIP}.tmp" \
                && [ -s "${TARGET_ZIP}.tmp" ]; then
                mv "${TARGET_ZIP}.tmp" "${TARGET_ZIP}"
                DOWNLOAD_OK=1
                break
            fi
            rm -f "${TARGET_ZIP}.tmp"
        done
    fi
    if [ "${DOWNLOAD_OK}" -ne 1 ]; then
        log_err "lxserver release zip 下载失败。可手动下载 ${LXSERVER_ZIP_NAME} 放置于 container/lxserver-artifact/ 后重跑。"
        exit 1
    fi
    if [ -n "${LXSERVER_SHA256:-}" ]; then
        ACTUAL_SHA="$(sha256sum "${TARGET_ZIP}" | awk '{print $1}')"
        if [ "${ACTUAL_SHA}" != "${LXSERVER_SHA256}" ]; then
            log_err "lxserver zip 校验和不符 (${ACTUAL_SHA} != ${LXSERVER_SHA256})。"
            exit 1
        fi
    fi
    rm -rf "${LXSERVER_DIR}.tmp"
    mkdir -p "${LXSERVER_DIR}.tmp"
    # unzip 非必装组件：用 python 标准库解压（与 pip 依赖同源，不新增系统依赖）
    python3 -m zipfile -e "${TARGET_ZIP}" "${LXSERVER_DIR}.tmp"
    if [ -d "${LXSERVER_DIR}.tmp/lx-music-sync-server" ]; then
        rm -rf "${LXSERVER_DIR}"
        mv "${LXSERVER_DIR}.tmp/lx-music-sync-server" "${LXSERVER_DIR}"
    else
        rm -rf "${LXSERVER_DIR}"
        mv "${LXSERVER_DIR}.tmp" "${LXSERVER_DIR}"
    fi
    rm -rf "${LXSERVER_DIR}.tmp"
    # 运行时入口是根级 index.js（supervisord: node index.js），必须存在；
    # v2.1.2 实际布局主脚本在 server/server/server.js，Dockerfile 的 getLyric
    # 补丁对 server/server.js 存在才打（同款语义，缺失时静默跳过）
    if [ ! -f "${LXSERVER_DIR}/index.js" ]; then
        log_err "lxserver 包结构异常：未找到入口 index.js。"
        exit 1
    fi
    if [ -f "${LXSERVER_DIR}/server/server.js" ]; then
        sed -i 's/const result = await musicSdk\[source\]\.getLyric(songInfo);/const _res = musicSdk[source].getLyric(songInfo); const result = await (_res \&\& _res.promise ? _res.promise : _res);/g' \
            "${LXSERVER_DIR}/server/server.js"
    fi
    printf '%s\n' "${LXSERVER_TAG}" > "${LXSERVER_STAMP}"
    log_info "lxserver ${LXSERVER_TAG} 已落位 ${LXSERVER_DIR}。"
fi

bash "${BASE_DIR}/container/ensure_lxserver_native.sh" \
    --artifact-dir "${LXSERVER_ARTIFACT_DIR}" \
    --lxserver-dir "${LXSERVER_DIR}" \
    --version-file "${LXSERVER_VERSION_FILE}"

log_info "原生音源运行时就绪。"
