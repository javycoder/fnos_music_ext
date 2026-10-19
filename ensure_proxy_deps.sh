#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 宿主机代理依赖保障（v2.1.0+）
# 背景：install.sh / extend.sh 为 .venv-proxy 安装 fastapi/uvicorn 等代理依赖时，
#       此前只使用单一 pip 源；部分用户网络到该源不可达（限流 403 / DNS /
#       路由 / 代理拦截），pip 报 "Could not find a version that satisfies
#       the requirement ... (from versions: none)" 且无回退，安装直接中断。
# 本脚本不修改任何系统配置，只负责：
#   1. 选择合格的宿主机 Python（显式配置 → fnOS python312 → 系统）并确保
#      .venv-proxy 存在；依赖未变化且校验通过时跳过联网安装
#   2. 按候选链安装：PIP_INDEX（默认阿里云，用户自定义永远第一位）
#      → 清华镜像 → 官方 PyPI（重复候选自动去重；每源带 --retries/--timeout 抗抖动）
#   3. 「升级 pip」与「安装 requirements」在同一候选源上连续完成，换源即整组重试
#   4. 全部候选源失败时报出排查指引并以非零退出
# 用法: bash ensure_proxy_deps.sh   （由 install.sh / extend.sh 调用，也可手动重跑）
# 可调环境变量: PIP_INDEX / FNMUSIC_VENV_DIR（默认 <脚本所在目录>/.venv-proxy，测试用）
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
    deps_requirements_digest() { sha256sum "$@" 2>/dev/null | awk '{print $1}' | sha256sum | awk '{print $1}'; }
    deps_state_valid() { return 1; }
    deps_write_state() { :; }
    deps_imports_ok() { return 0; }
fi
VENV_DIR="${FNMUSIC_VENV_DIR:-${BASE_DIR}/.venv-proxy}"
REQ_FILE="${BASE_DIR}/proxy/requirements.txt"
FNMUSIC_DEPS_REVISION="proxy-20261019-1"
PIP_INDEX="${PIP_INDEX:-https://mirrors.tencent.com/pypi/simple/}"
FALLBACK_INDEXES="https://mirrors.aliyun.com/pypi/simple/ https://pypi.tuna.tsinghua.edu.cn/simple https://pypi.org/simple"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

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
    log_err "可删除 ${VENV_DIR} 后重跑本脚本（或 install.sh）重建。"
    exit 1
fi
if ! runtime_python_ok "${VENV_PYTHON}"; then
    log_err "虚拟环境 Python 不满足 3.11+ 或缺少 ensurepip/ssl/sqlite3。"
    log_err "请删除 ${VENV_DIR} 后重跑本脚本重建。"
    exit 1
fi

REQ_DIGEST="$(deps_requirements_digest "${REQ_FILE}")"
if [ "${FNMUSIC_REFRESH_DEPS:-0}" != "1" ] \
   && deps_state_valid "${VENV_DIR}" "${FNMUSIC_DEPS_REVISION}" "${VENV_PYTHON}" \
        "${REQ_DIGEST}" fastapi uvicorn httpx mutagen; then
    log_info "代理依赖已就绪，跳过联网安装。"
    exit 0
fi
rm -f "${VENV_DIR}/.fnmusic-deps"

# 候选源去重：用户自定义 PIP_INDEX 永远第一位
candidates="${PIP_INDEX}"
for idx in ${FALLBACK_INDEXES}; do
    [ "${idx}" = "${PIP_INDEX}" ] || candidates="${candidates} ${idx}"
done

ok_index=""
for idx in ${candidates}; do
    log_info "尝试 pip 源: ${idx}"
    if "${PIP_BIN}" install -q -U pip --retries 2 --timeout 30 -i "${idx}" \
       && "${PIP_BIN}" install -q -r "${REQ_FILE}" --retries 2 --timeout 30 -i "${idx}"; then
        ok_index="${idx}"
        break
    fi
    log_warn "pip 源不可用: ${idx}，切换下一候选源重试..."
done

if [ -z "${ok_index}" ]; then
    log_err "所有 pip 源均安装失败（尝试了: ${candidates}）。"
    log_err "排查建议：1) 检查宿主机网络 / DNS / 代理（含 http_proxy、https_proxy 环境变量）；"
    log_err "           2) 换源重试: PIP_INDEX=<其他源URL> ./install.sh（或 extend.sh）。"
    exit 1
fi
if ! "${PIP_BIN}" check >/dev/null 2>&1 \
   || ! deps_imports_ok "${VENV_PYTHON}" fastapi uvicorn httpx mutagen; then
    log_err "依赖安装后校验失败（pip check 或关键模块导入失败）。"
    exit 1
fi
deps_write_state "${VENV_DIR}" "${FNMUSIC_DEPS_REVISION}" "${VENV_PYTHON}" "${REQ_DIGEST}"
log_info "代理依赖安装完成（pip 源: ${ok_index}）。"
