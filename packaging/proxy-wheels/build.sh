#!/usr/bin/env bash
set -euo pipefail

# 为预构建 FPK 下载宿主机代理依赖 wheel。
# 目标固定为 CPython 3.11、3.12、3.13 的指定架构；纯 Python wheel 会自然复用。
# 用法: packaging/proxy-wheels/build.sh <amd64|arm64> <dest> <requirements.txt>

ARCH="${1:?用法: $0 <amd64|arm64> <dest> <requirements>}"
DEST="${2:?用法: $0 <amd64|arm64> <dest> <requirements>}"
REQ="${3:?用法: $0 <amd64|arm64> <dest> <requirements>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "${ARCH}" in
    amd64)
        platforms=(manylinux_2_28_x86_64 manylinux_2_17_x86_64 manylinux2014_x86_64)
        ;;
    arm64)
        platforms=(manylinux_2_28_aarch64 manylinux_2_17_aarch64 manylinux2014_aarch64)
        ;;
    *)
        echo "ERROR: 不支持的 wheel 架构 ${ARCH}" >&2
        exit 1
        ;;
esac

rm -rf "${DEST}"
mkdir -p "${DEST}"
platform_args=()
for platform in "${platforms[@]}"; do
    platform_args+=(--platform "${platform}")
done

for py in 311 312 313; do
    echo "[wheels] 下载 CPython ${py} / ${ARCH}"
    python3 -m pip download \
        --requirement "${REQ}" \
        --dest "${DEST}" \
        --only-binary=:all: \
        --python-version "${py}" \
        --implementation cp \
        --abi "cp${py}" --abi abi3 --abi none \
        "${platform_args[@]}"
done

python3 "${ROOT}/proxy/wheelhouse.py" write \
    --dest "${DEST}" --arch "${ARCH}" --requirements "${REQ}"
python3 "${ROOT}/proxy/wheelhouse.py" check \
    --manifest "${DEST}/manifest.json" --requirements "${REQ}"
echo "[wheels] 宿主 wheelhouse 就绪: ${DEST}"
