#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# lxserver 原生标签模块架构补齐
# 上游 v2.1.2 server.zip 只内嵌 linux-x64-gnu 绑定，ARM64 启动即报：
# Cannot find module music-tag-native-linux-arm64-gnu。此脚本按构建机架构
# 下载并落位锁定的 npm 平台包，Docker、原生与 FPK 离线安装共用同一校验。
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="${BASE_DIR}/container/lxserver.version"
ARTIFACT_DIR="${BASE_DIR}/container/lxserver-artifact"
LXSERVER_DIR="${BASE_DIR}/.lxserver"
REQUIRED_ARCH="${FNMUSIC_LXSERVER_ARCH:-}"
DOWNLOAD_ONLY=0

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --artifact-dir) ARTIFACT_DIR="$2"; shift 2 ;;
        --download-only) DOWNLOAD_ONLY=1; shift ;;
        --lxserver-dir) LXSERVER_DIR="$2"; shift 2 ;;
        --required-arch) REQUIRED_ARCH="$2"; shift 2 ;;
        --version-file) VERSION_FILE="$2"; shift 2 ;;
        *)
            echo "用法: $0 [--artifact-dir DIR] [--lxserver-dir DIR] [--required-arch x64|arm64] [--version-file FILE] [--download-only]" >&2
            exit 2
            ;;
    esac
done

if [ ! -f "${VERSION_FILE}" ]; then
    log_err "缺少版本锁定文件: ${VERSION_FILE}"
    exit 1
fi
# shellcheck disable=SC1090
. "${VERSION_FILE}"

case "${REQUIRED_ARCH:-auto}" in
    auto)
        case "$(uname -m)" in
            x86_64|amd64) REQUIRED_ARCH="x64" ;;
            aarch64|arm64) REQUIRED_ARCH="arm64" ;;
            *) REQUIRED_ARCH="" ;;
        esac
        ;;
esac

if [ "${REQUIRED_ARCH}" = "x64" ]; then
    NATIVE_NAME="music-tag-native.linux-x64-gnu.node"
    PACKAGE_DIR="music-tag-native-linux-x64-gnu"
elif [ "${REQUIRED_ARCH}" = "arm64" ]; then
    NATIVE_NAME="music-tag-native.linux-arm64-gnu.node"
    PACKAGE_DIR="music-tag-native-linux-arm64-gnu"
else
    log_err "当前架构暂不支持运行 lxserver: $(uname -m)（支持 x64/arm64）"
    exit 1
fi

NATIVE_VERSION="${LXSERVER_NATIVE_VERSION:-0.2.5}"
NATIVE_SHA256="${LXSERVER_NATIVE_SHA256:-}"
TARBALL_NAME="${PACKAGE_DIR}-${NATIVE_VERSION}.tgz"
if [ "${REQUIRED_ARCH}" = "arm64" ]; then
    NATIVE_SHA256="${LXSERVER_NATIVE_ARM64_SHA256:-${NATIVE_SHA256}}"
elif [ -z "${NATIVE_SHA256}" ]; then
    # x64 绑定由上游 server.zip 自带；缺失说明上游构件损坏，不能静默替换。
    NATIVE_SHA256="upstream"
fi

if [ "${REQUIRED_ARCH}" = "arm64" ] && [ -z "${NATIVE_SHA256}" ]; then
    log_err "版本锁定文件缺少 LXSERVER_NATIVE_ARM64_SHA256，拒绝安装未校验模块。"
    exit 1
fi

if [ "${DOWNLOAD_ONLY}" -ne 1 ]; then
    PACKAGE_NATIVE="${LXSERVER_DIR}/node_modules/music-tag-native/${NATIVE_NAME}"
    NAMED_NATIVE="${LXSERVER_DIR}/node_modules/${PACKAGE_DIR}/${NATIVE_NAME}"
    if [ -s "${PACKAGE_NATIVE}" ] || [ -s "${NAMED_NATIVE}" ]; then
        log_info "lxserver ${REQUIRED_ARCH} 原生模块已就位，跳过。"
        exit 0
    fi
    if [ "${REQUIRED_ARCH}" = "x64" ]; then
        log_err "上游 server.zip 缺少 x64 原生模块: ${NATIVE_NAME}"
        exit 1
    fi
fi

if [ "${REQUIRED_ARCH}" != "arm64" ]; then
    exit 0
fi

mkdir -p "${ARTIFACT_DIR}"
TARBALL="${ARTIFACT_DIR}/${TARBALL_NAME}"
if [ ! -s "${TARBALL}" ]; then
    PACKAGE_PATH="${PACKAGE_DIR}/-/${TARBALL_NAME}"
    MIRRORS=(
        "https://registry.npmjs.org"
        "https://registry.npmmirror.com"
    )
    DOWNLOAD_OK=0
    TEMP_FILE="${TARBALL}.tmp"
    for registry in "${MIRRORS[@]}"; do
        URL="${registry}/${PACKAGE_PATH}"
        log_info "尝试下载 ${PACKAGE_DIR}@${NATIVE_VERSION}: ${URL}"
        if curl -fSL --connect-timeout 10 --max-time 120 "${URL}" -o "${TEMP_FILE}" && [ -s "${TEMP_FILE}" ]; then
            mv -f "${TEMP_FILE}" "${TARBALL}"
            DOWNLOAD_OK=1
            break
        fi
        rm -f "${TEMP_FILE}"
    done
    if [ "${DOWNLOAD_OK}" -ne 1 ]; then
        log_err "下载 ${PACKAGE_DIR}@${NATIVE_VERSION} 失败。"
        log_err "可手动下载后放置: ${TARBALL}"
        exit 1
    fi
fi

ACTUAL_SHA="$(sha256sum "${TARBALL}" | awk '{print $1}')"
if [ "${ACTUAL_SHA}" != "${NATIVE_SHA256}" ]; then
    log_err "lxserver 原生模块包校验和不符 (${ACTUAL_SHA} != ${NATIVE_SHA256})"
    exit 1
fi

if [ "${DOWNLOAD_ONLY}" -ne 1 ]; then
    TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lxserver-native-XXXXXXXX")"
    trap 'rm -rf "${TEMP_DIR}"' EXIT
    python3 - "${TARBALL}" "${TEMP_DIR}" <<'PY'
import sys
import tarfile
from pathlib import Path

with tarfile.open(sys.argv[1], "r:gz") as archive:
    destination = Path(sys.argv[2]).resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if destination not in target.parents or not member.isreg():
            raise ValueError(f"unsafe native package member: {member.name}")
    archive.extractall(sys.argv[2])
PY
    NAMED_DEST="${LXSERVER_DIR}/node_modules/${PACKAGE_DIR}"
    mkdir -p "${NAMED_DEST}"
    cp -a "${TEMP_DIR}/package/." "${NAMED_DEST}/"
    mkdir -p "${LXSERVER_DIR}/node_modules/music-tag-native"
    cp -f "${NAMED_DEST}/${NATIVE_NAME}" "${LXSERVER_DIR}/node_modules/music-tag-native/${NATIVE_NAME}"
    log_info "lxserver ${REQUIRED_ARCH} 原生模块已补齐。"
fi
