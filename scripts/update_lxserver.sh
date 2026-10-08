#!/usr/bin/env bash
# scripts/update_lxserver.sh - 更新或预下载 lxserver release 包
# 用法:
#   ./scripts/update_lxserver.sh [TAG]
#   例如: ./scripts/update_lxserver.sh v2.1.2
# 若省略 TAG，默认读取 container/lxserver.version 中的配置并保证本地 artifact 存在。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="${REPO_ROOT}/container/lxserver.version"
ARTIFACT_DIR="${REPO_ROOT}/container/lxserver-artifact"

mkdir -p "${ARTIFACT_DIR}"

TARGET_TAG="${1:-}"
if [ -z "${TARGET_TAG}" ]; then
    if [ -f "${VERSION_FILE}" ]; then
        # shellcheck disable=SC1090
        source "${VERSION_FILE}"
        TARGET_TAG="${LXSERVER_TAG:-v2.1.2}"
        EXPECTED_SHA="${LXSERVER_SHA256:-}"
    else
        TARGET_TAG="v2.1.2"
        EXPECTED_SHA=""
    fi
else
    EXPECTED_SHA=""
fi

ZIP_NAME="lx-music-sync-server-${TARGET_TAG}-server.zip"
TARGET_FILE="${ARTIFACT_DIR}/${ZIP_NAME}"

# 若本地已存在且符合预期 sha256，则跳过下载
if [ -f "${TARGET_FILE}" ] && [ -n "${EXPECTED_SHA}" ]; then
    ACTUAL_SHA="$(sha256sum "${TARGET_FILE}" | awk '{print $1}')"
    if [ "${ACTUAL_SHA}" = "${EXPECTED_SHA}" ]; then
        echo "[INFO] 本地已存在符合校验和的 lxserver 预编译包: ${TARGET_FILE}"
        exit 0
    else
        echo "[WARN] 本地预编译包 sha256 不符，将重新下载 (${ACTUAL_SHA} != ${EXPECTED_SHA})"
    fi
fi

echo "[INFO] 准备拉取 lxserver ${TARGET_TAG} 预编译包..."

MIRRORS=(
    "https://ghfast.top"
    "https://gh-proxy.com"
    "https://github.moeyy.xyz"
    ""
)

BASE_RELEASE="https://github.com/XCQ0607/lxserver/releases/download/${TARGET_TAG}/${ZIP_NAME}"
DOWNLOAD_OK=0
TEMP_FILE="$(mktemp -p "${ARTIFACT_DIR}" .tmp-lxserver-XXXXXX.zip)"

cleanup() {
    rm -f "${TEMP_FILE}"
}
trap cleanup EXIT

for mirror in "${MIRRORS[@]}"; do
    if [ -n "${mirror}" ]; then
        URL="${mirror}/${BASE_RELEASE}"
    else
        URL="${BASE_RELEASE}"
    fi
    echo "[INFO] 尝试下载: ${URL}"
    if curl -fSL --connect-timeout 10 --max-time 300 "${URL}" -o "${TEMP_FILE}"; then
        if [ -s "${TEMP_FILE}" ]; then
            DOWNLOAD_OK=1
            echo "[INFO] 下载成功: ${URL}"
            break
        fi
    fi
    echo "[WARN] 当前镜像拉取失败，尝试下一个..."
done

if [ "${DOWNLOAD_OK}" -ne 1 ]; then
    echo "ERROR: 所有镜像均拉取失败，请检查网络或手动下载 ${BASE_RELEASE} 到 ${TARGET_FILE}" >&2
    exit 1
fi

CALCULATED_SHA="$(sha256sum "${TEMP_FILE}" | awk '{print $1}')"

if [ -n "${EXPECTED_SHA}" ] && [ "${CALCULATED_SHA}" != "${EXPECTED_SHA}" ]; then
    echo "ERROR: sha256 校验失败 (计算: ${CALCULATED_SHA}, 期望: ${EXPECTED_SHA})" >&2
    exit 1
fi

mv "${TEMP_FILE}" "${TARGET_FILE}"
chmod 0644 "${TARGET_FILE}"

# 更新锁定文件
cat <<EOF > "${VERSION_FILE}"
# lxserver 版本锁定与完整性校验文件
# 由 scripts/update_lxserver.sh 维护；Dockerfile 构建与 CI 打包时自动校验
LXSERVER_TAG=${TARGET_TAG}
LXSERVER_ZIP_NAME=${ZIP_NAME}
LXSERVER_SHA256=${CALCULATED_SHA}
EOF

echo "[INFO] lxserver ${TARGET_TAG} 更新成功！"
echo "[INFO] 文件: ${TARGET_FILE}"
echo "[INFO] SHA256: ${CALCULATED_SHA}"
