#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext Proxy 启动入口 (以 root 权限由 systemd 调用)
# 架构：Unix Socket 接管
#
# 幂等接管：
# - TARGET 是 trim-music → mv 到 UPSTREAM，再绑代理
# - UPSTREAM 已是 trim-music（systemd 重启场景）→ 只替换 TARGET 上的代理 socket
# 禁止 rm 掉仍在 accept 的 trim-music inode。
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

TARGET_SOCK="/var/run/trim_music.socket"
UPSTREAM_SOCK="/var/run/trim_music_upstream.socket"
CACHE_DIR="${BASE_DIR}/cache"

probe_sock() {
    local sock="$1"
    if [ ! -S "${sock}" ]; then
        echo "none"
        return
    fi
    local health
    health="$(curl -s --max-time 2 --unix-socket "${sock}" http://localhost/_ext/healthz 2>/dev/null || true)"
    if echo "${health}" | grep -q '"upstream"'; then
        echo "proxy"
        return
    fi
    local search
    search="$(curl -s --max-time 2 --unix-socket "${sock}" "http://localhost/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
    if echo "${search}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
        echo "trim"
        return
    fi
    echo "stale"
}

echo "[run_proxy] 等待 trim-music 在 ${TARGET_SOCK} 或 ${UPSTREAM_SOCK} 上就绪..."
FOUND_TRIM=0
for i in $(seq 1 60); do
    t_probe="$(probe_sock "${TARGET_SOCK}")"
    u_probe="$(probe_sock "${UPSTREAM_SOCK}")"
    if [ "${t_probe}" = "trim" ] || [ "${u_probe}" = "trim" ]; then
        FOUND_TRIM=1
        break
    fi
    sleep 1
done

if [ "${FOUND_TRIM}" -ne 1 ]; then
    echo "[run_proxy] 错误: 未探测到存活的 trim-music socket（TARGET=${t_probe} UPSTREAM=${u_probe}）" >&2
    exit 1
fi

t_probe="$(probe_sock "${TARGET_SOCK}")"
u_probe="$(probe_sock "${UPSTREAM_SOCK}")"
echo "[run_proxy] probe TARGET=${t_probe} UPSTREAM=${u_probe}"

if [ "${t_probe}" = "trim" ]; then
    if [ "${u_probe}" != "none" ] && [ "${u_probe}" != "trim" ]; then
        echo "[run_proxy] 清理非 trim 的 upstream 残留 (${u_probe})..."
        rm -f "${UPSTREAM_SOCK}"
    fi
    if [ "${u_probe}" = "trim" ]; then
        echo "[run_proxy] TARGET 与 UPSTREAM 都是 trim-music，保留 UPSTREAM，移除 TARGET 后接管"
        rm -f "${TARGET_SOCK}"
    else
        echo "[run_proxy] 执行 socket 接管: ${TARGET_SOCK} -> ${UPSTREAM_SOCK}"
        mv "${TARGET_SOCK}" "${UPSTREAM_SOCK}"
    fi
elif [ "${u_probe}" = "trim" ]; then
    echo "[run_proxy] UPSTREAM 已是 trim-music，仅替换代理 socket"
    if [ "${t_probe}" != "none" ]; then
        rm -f "${TARGET_SOCK}"
    fi
else
    echo "[run_proxy] 错误: 无 trim-music 可接管" >&2
    exit 1
fi

mkdir -p "${CACHE_DIR}"

export FNMUSIC_HOME="${BASE_DIR}"
export FNMUSIC_UPSTREAM_SOCK="${UPSTREAM_SOCK}"
export FNMUSIC_CACHE_DIR="${CACHE_DIR}"
export PYTHONUNBUFFERED=1

if [ -f "${BASE_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${BASE_DIR}/.env"
    set +a
fi

UVICORN="${BASE_DIR}/.venv-proxy/bin/uvicorn"
if [ ! -x "${UVICORN}" ]; then
    UVICORN="uvicorn"
fi

echo "[run_proxy] 启动 uvicorn 代理服务绑定至 ${TARGET_SOCK}..."
exec "${UVICORN}" app:app \
    --app-dir "${BASE_DIR}/proxy" \
    --uds "${TARGET_SOCK}"
