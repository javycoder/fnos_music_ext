#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext fpk 打包脚本（本地与 CI 共用）
# 组装应用骨架 + 仓库主体 → fnpack build → dist/fnmusic-ext-<版本>.fpk
#
# 用法:
#   packaging/fpk/build.sh                     # 完整打包（fnpack 缺失时自动下载）
#   packaging/fpk/build.sh --stage-only DIR    # 只组装不打包（结构测试用）
#   packaging/fpk/build.sh --fnpack /path/fnpack --out dist
# 环境变量: FNPACK_VERSION（默认 1.2.3）
#           FPK_IMAGE_ASSETS=require|auto|skip（默认 auto；CI 用 require 强制内嵌
#           预构建应用镜像归档，见下方 1.5 节）与 FPK_IMAGE_ARCH / FPK_IMAGE_BASE /
#           FPK_IMAGE_PIP_INDEX / FPK_IMAGE_APT_MIRROR / FPK_IMAGE_GZIP_LEVEL
# ==============================================================================

FPK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${FPK_DIR}/../.." && pwd)"
VERSION="$(python3 "${REPO_ROOT}/proxy/version.py" --format version)"
if [ -z "${VERSION}" ]; then
    echo "ERROR: 无法读取 ${REPO_ROOT}/VERSION" >&2
    exit 1
fi
python3 "${REPO_ROOT}/scripts/check_music_compat.py" --root "${REPO_ROOT}"

FNPACK_BIN="${FNPACK:-}"
STAGE=""
STAGE_ONLY=0
OUT_DIR="${REPO_ROOT}/dist"

while [ $# -gt 0 ]; do
    case "$1" in
        --stage-only)
            [ $# -ge 2 ] || { echo "用法: $0 --stage-only <dir>" >&2; exit 1; }
            STAGE_ONLY=1
            STAGE="$2"
            shift 2
            ;;
        --fnpack)
            [ $# -ge 2 ] || { echo "用法: $0 --fnpack <path>" >&2; exit 1; }
            FNPACK_BIN="$2"
            shift 2
            ;;
        --out)
            [ $# -ge 2 ] || { echo "用法: $0 --out <dir>" >&2; exit 1; }
            OUT_DIR="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 1
            ;;
    esac
done

if [ -z "${STAGE}" ]; then
    STAGE="${FPK_DIR}/.build/fpk"
fi

command -v rsync >/dev/null 2>&1 || {
    echo "ERROR: 需要 rsync（Debian: sudo apt-get install -y rsync）" >&2
    exit 1
}

# 确保 lxserver 预编译包已在构建缓存就绪（内嵌到 fpk 中实现 NAS 用户离线安装）
BUILD_CACHE="${FPK_DIR}/.build/cache"
mkdir -p "${BUILD_CACHE}"
if [ -f "${REPO_ROOT}/scripts/update_lxserver.sh" ]; then
    echo "[fpk] 检查构建缓存中的 lxserver 预编译包..."
    bash "${REPO_ROOT}/scripts/update_lxserver.sh" "" "${BUILD_CACHE}" || echo "[WARN] 缓存预编译包检查失败"
fi
echo "[fpk] 检查 lxserver ARM64 原生模块..."
if ! bash "${REPO_ROOT}/container/ensure_lxserver_native.sh" \
    --artifact-dir "${BUILD_CACHE}" \
    --version-file "${REPO_ROOT}/container/lxserver.version" \
    --required-arch arm64 --download-only; then
    if [ "${STAGE_ONLY}" -eq 1 ]; then
        echo "[WARN] 离线组装缺少 ARM64 原生模块；正式打包会强制失败"
    else
        exit 1
    fi
fi

# ------------------------------------------------------------------------------
# 1. 组装打包目录
# ------------------------------------------------------------------------------
rm -rf "${STAGE}"
mkdir -p "${STAGE}/app/repo" "${STAGE}/app/ui"

# 仓库主体（排除开发文件与本地秘密；.env.example 保留作模板）
rsync -a --delete \
    --include='.env.example' \
    --exclude='.env' --exclude='.env.*' \
    --exclude='.git/' --exclude='.git*' \
    --exclude='.github/' --exclude='.zcode/' --exclude='.cursor/' --exclude='.agents/' \
    --exclude='.tasks_archive/' --exclude='.tools/' \
    --exclude='packaging/' --exclude='dist/' \
    --exclude='tests/' --exclude='docs/' --exclude='pytest.ini' \
    --exclude='CONTRIBUTING.md' --exclude='AGENTS.md' \
    --exclude='test_*.py' --exclude='test_*.js' --exclude='conftest.py' --exclude='smoke_test.sh' --exclude='run_tests.py' \
    --exclude='.venv*/' --exclude='__pycache__/' --exclude='.pytest_cache/' \
    --exclude='*.pyc' --exclude='*.log' --exclude='*.logzip' --exclude='logs/' --exclude='/logs' \
    --exclude='*.part' --exclude='.DS_Store' \
    --exclude='cache/' --exclude='sources-data/' --exclude='backup/' \
    --exclude='musicbox-data/' --exclude='musicdl_outputs/' \
    --exclude='recommend_cache/' --exclude='public_playlists_cache/' --exclude='play_history/' \
    --exclude='online_favorites/' --exclude='online_favorites.json' \
    --exclude='llm_usage.json' --exclude='llm_usage.json.*' \
    --exclude='playlist_tracks/' --exclude='album_registry.json' --exclude='bind_registry.json' \
    --exclude='*.fpk' \
    "${REPO_ROOT}/" "${STAGE}/app/repo/"

# 注入预编译包至打包 staging 目录，实现 fpk 离线内嵌而不污染源码树
if [ -f "${REPO_ROOT}/container/lxserver.version" ]; then
    # shellcheck disable=SC1090
    source "${REPO_ROOT}/container/lxserver.version"
    CACHED_ZIP="${BUILD_CACHE}/${LXSERVER_ZIP_NAME:-}"
    if [ -f "${CACHED_ZIP}" ]; then
        mkdir -p "${STAGE}/app/repo/container/lxserver-artifact"
        cp -p "${CACHED_ZIP}" "${STAGE}/app/repo/container/lxserver-artifact/"
        echo "[fpk] 已将离线预编译包注入至打包目标目录: ${LXSERVER_ZIP_NAME}"
    fi
    ARM64_NATIVE="${LXSERVER_NATIVE_VERSION:+music-tag-native-linux-arm64-gnu-${LXSERVER_NATIVE_VERSION}.tgz}"
    if [ -n "${ARM64_NATIVE}" ] && [ -f "${BUILD_CACHE}/${ARM64_NATIVE}" ]; then
        cp -p "${BUILD_CACHE}/${ARM64_NATIVE}" "${STAGE}/app/repo/container/lxserver-artifact/"
        echo "[fpk] 已将 ARM64 原生模块注入至打包目标目录: ${ARM64_NATIVE}"
    elif [ "${STAGE_ONLY}" -eq 1 ]; then
        echo "[WARN] 打包目标目录缺少 ARM64 原生模块: ${ARM64_NATIVE:-}"
    else
        echo "ERROR: 缺少 ARM64 原生模块 ${ARM64_NATIVE:-}" >&2
        exit 1
    fi
fi

# ------------------------------------------------------------------------------
# 1.5 应用镜像离线资产（3.0.0+）
# CI/正式打包：从最终 staging 构建 docker 镜像 → docker save | gzip → app/repo/assets/image/
# 安装端 proxy/image_bundle.py 优先复用/导入该归档，NAS 正常安装不再现场执行
# apt/pip BuildKit 构建（2026-11-03 实测该段占 261s 安装的约 84%）。
# FPK_IMAGE_ASSETS: require（CI：Docker 不可用即失败）| auto（默认：本机有 Docker
# 才构建，--stage-only 结构测试不受影响）| skip（显式跳过）
# FPK_IMAGE_ARCH: 目标架构（默认本机）；arm64 交叉构建需 buildx + QEMU
# ------------------------------------------------------------------------------
# --stage-only 结构测试默认不构建镜像（CI 的 ubuntu runner 有 Docker，auto 会
# 意外触发数分钟构建）；显式 FPK_IMAGE_ASSETS=require 仍可在 stage-only 强制构建。
if [ "${STAGE_ONLY}" -eq 1 ] && [ -z "${FPK_IMAGE_ASSETS:-}" ]; then
    IMAGE_ASSET_MODE="skip"
else
    IMAGE_ASSET_MODE="${FPK_IMAGE_ASSETS:-auto}"
fi
ASSET_ARCH_SUFFIX=""
if [ "${IMAGE_ASSET_MODE}" != "skip" ]; then
    case "$(uname -m)" in
        x86_64) _HOST_ARCH=amd64 ;;
        aarch64|arm64) _HOST_ARCH=arm64 ;;
        *) _HOST_ARCH="" ;;
    esac
    IMAGE_ARCH="${FPK_IMAGE_ARCH:-${_HOST_ARCH}}"
    if [ -n "${IMAGE_ARCH}" ] && docker info >/dev/null 2>&1; then
        BUNDLE_DIR="${STAGE}/app/repo/assets/image"
        mkdir -p "${BUNDLE_DIR}"
        echo "[fpk] 从最终 staging 构建应用镜像归档 (linux/${IMAGE_ARCH})..."
        GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
        # buildx 默认 attestation 会让产物成为 manifest list，inspect/save 拿不到
        # RootFS.DiffIds；需要经典单平台镜像（支持的 docker 上显式关闭）。
        ASSET_BUILD_FLAGS=()
        if docker build --help 2>&1 | grep -q -- '--provenance'; then
            ASSET_BUILD_FLAGS+=(--provenance=false --sbom=false)
        fi
        DOCKER_BUILDKIT=1 docker build \
            --platform "linux/${IMAGE_ARCH}" \
            -f "${STAGE}/app/repo/container/Dockerfile" \
            --build-arg BASE_IMAGE="${FPK_IMAGE_BASE:-python:3.13-slim}" \
            --build-arg PIP_INDEX_URL="${FPK_IMAGE_PIP_INDEX:-https://pypi.org/simple}" \
            --build-arg APT_MIRROR="${FPK_IMAGE_APT_MIRROR:-}" \
            "${ASSET_BUILD_FLAGS[@]}" \
            -t fnmusic-sources:latest \
            "${STAGE}/app/repo"
        IMAGE_ID="$(docker image inspect fnmusic-sources:latest --format '{{.Id}}')"
        DIFF_IDS="$(docker image inspect fnmusic-sources:latest --format '{{json .RootFS.DiffIds}}')"
        DOCKERFILE_SHA="$(sha256sum "${STAGE}/app/repo/container/Dockerfile" | awk '{print $1}')"
        ARCHIVE="${BUNDLE_DIR}/fnmusic-sources-image.tar.gz"
        echo "[fpk] docker save | gzip → ${ARCHIVE} ..."
        docker save fnmusic-sources:latest | gzip "-${FPK_IMAGE_GZIP_LEVEL:-6}" > "${ARCHIVE}"
        ARCHIVE_SHA="$(sha256sum "${ARCHIVE}" | awk '{print $1}')"
        ARCHIVE_BYTES="$(stat -c %s "${ARCHIVE}")"
        python3 - "${BUNDLE_DIR}" "${IMAGE_ARCH}" "${IMAGE_ID}" "${DIFF_IDS}" \
            "${ARCHIVE_SHA}" "${ARCHIVE_BYTES}" "${DOCKERFILE_SHA}" "${VERSION}" "${GIT_COMMIT}" <<'PY'
import datetime, json, pathlib, sys
(bundle, arch, image_id, diff_ids, sha, size, dockerfile_sha, version, commit) = sys.argv[1:]
manifest = {
    "schema": 1,
    "app": "fnmusic-ext",
    "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "arch": arch,
    "platform": f"linux/{arch}",
    "image_tag": "fnmusic-sources:latest",
    "image_id": image_id,
    "rootfs_diff_ids": json.loads(diff_ids),
    "archive": "fnmusic-sources-image.tar.gz",
    "archive_sha256": sha,
    "archive_bytes": int(size),
    "dockerfile_sha256": dockerfile_sha,
    "app_version": version,
    "source_commit": commit,
}
pathlib.Path(bundle, "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
        echo "[fpk] 镜像资产就绪: ${IMAGE_ID#sha256:} 归档 $(du -m "${ARCHIVE}" | cut -f1)MB"
        # 内嵌镜像的 FPK 按架构命名，避免双架构产物同名混淆（manifest 内版本号不变）
        ASSET_ARCH_SUFFIX="-${IMAGE_ARCH}"
    elif [ "${IMAGE_ASSET_MODE}" = "require" ]; then
        echo "ERROR: FPK_IMAGE_ASSETS=require，但 Docker 不可用或无法识别架构" >&2
        exit 1
    else
        echo "[WARN] Docker 不可用，跳过镜像资产构建；该 FPK 安装时将回落现场构建路径。"
    fi
fi

# 正式和预发布的 manifest、包内 VERSION、文件名均保留数字 -N；通道另存 RELEASE_CHANNEL。
sed "s/@VERSION@/${VERSION}/" "${FPK_DIR}/manifest.in" > "${STAGE}/manifest"
cp "${FPK_DIR}/ICON.PNG" "${FPK_DIR}/ICON_256.PNG" "${STAGE}/"
cp -a "${FPK_DIR}/app/ui/." "${STAGE}/app/ui/"
cp -a "${FPK_DIR}/cmd" "${STAGE}/cmd"
cp -a "${FPK_DIR}/config" "${STAGE}/config"
cp -a "${FPK_DIR}/wizard" "${STAGE}/wizard"
chmod +x "${STAGE}"/cmd/*

echo "[fpk] 组装完成: ${STAGE} (version=${VERSION})"

if [ "${STAGE_ONLY}" -eq 1 ]; then
    exit 0
fi

# ------------------------------------------------------------------------------
# 2. fnpack 打包
# ------------------------------------------------------------------------------
if [ -z "${FNPACK_BIN}" ]; then
    if command -v fnpack >/dev/null 2>&1; then
        FNPACK_BIN="$(command -v fnpack)"
    else
        case "$(uname -m)" in
            x86_64) FNARCH=amd64 ;;
            aarch64|arm64) FNARCH=arm64 ;;
            *) echo "ERROR: 不支持的架构 $(uname -m)" >&2; exit 1 ;;
        esac
        FNPACK_VERSION="${FNPACK_VERSION:-1.2.3}"
        TOOLS_DIR="${REPO_ROOT}/.tools"
        FNPACK_BIN="${TOOLS_DIR}/fnpack"
        if [ ! -x "${FNPACK_BIN}" ] || ! "${FNPACK_BIN}" --help >/dev/null 2>&1; then
            mkdir -p "${TOOLS_DIR}"
            echo "[fpk] 下载 fnpack ${FNPACK_VERSION} (linux-${FNARCH})..."
            curl -fsSL -o "${FNPACK_BIN}" \
                "https://static2.fnnas.com/fnpack/fnpack-${FNPACK_VERSION}-linux-${FNARCH}"
            chmod +x "${FNPACK_BIN}"
        fi
    fi
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
if ! (cd "${WORK_DIR}" && "${FNPACK_BIN}" build --directory "${STAGE}"); then
    echo "ERROR: fnpack build 失败" >&2
    exit 1
fi

BUILT="$(find "${WORK_DIR}" "${STAGE}" -maxdepth 2 -name '*.fpk' -print -quit 2>/dev/null || true)"
if [ -z "${BUILT}" ]; then
    echo "ERROR: 未找到 fnpack 生成的 .fpk 文件" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"
FINAL="${OUT_DIR}/fnmusic-ext-${VERSION}${ASSET_ARCH_SUFFIX}.fpk"
mv "${BUILT}" "${FINAL}"
( cd "$(dirname "${FINAL}")" && sha256sum "$(basename "${FINAL}")" > "$(basename "${FINAL}").sha256" )

echo "[fpk] 打包完成: ${FINAL}"
