#!/usr/bin/env bash
set -uo pipefail

# ==============================================================================
# fnmusic-ext 网易云终端扫码登录脚本
# 功能：终端展示 ASCII 二维码、过期自动刷新、登录状态轮询
# 依赖：bash, curl, jq（严禁引入 python 依赖）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

handle_interrupt() {
    echo ""
    echo "已跳过登录。之后可随时执行 ./install.sh --qr 或 ./netease_login.sh 重新扫码"
    exit 0
}

trap handle_interrupt INT TERM

# 优先取环境变量，其次从 .env 读取，默认 127.0.0.1:8770
MUSICBOX_URL="${FNMUSIC_MUSICBOX_URL:-}"
if [ -z "${MUSICBOX_URL}" ] && [ -f "${BASE_DIR}/.env" ]; then
    MUSICBOX_URL="$(grep -E '^FNMUSIC_MUSICBOX_URL=' "${BASE_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d "'\"\r " || true)"
fi
MUSICBOX_URL="${MUSICBOX_URL:-http://127.0.0.1:8770}"

# 1. 检查 musicbox 音源服务连通性
if ! curl -s --max-time 5 -f "${MUSICBOX_URL}/healthz" >/dev/null 2>&1; then
    echo "musicbox 音源未运行，请先安装"
    exit 0
fi

# 2. 检查当前是否已登录
status_resp="$(curl -s --max-time 5 "${MUSICBOX_URL}/api/v1/auth/status" || true)"
logged_in="$(echo "${status_resp}" | jq -r '.data.logged_in // false' 2>/dev/null || echo "false")"
if [ "${logged_in}" = "true" ]; then
    nickname="$(echo "${status_resp}" | jq -r '.data.nickname // empty' 2>/dev/null || true)"
    [ -n "${nickname}" ] || nickname="$(echo "${status_resp}" | jq -r '.data.user_id // empty' 2>/dev/null || true)"
    [ -n "${nickname}" ] || nickname="已登录用户"
    echo "✅ 网易云已登录：${nickname}，无需重复登录"
    exit 0
fi

START_TIME="$(date +%s)"
MAX_TIMEOUT=900 # 15 分钟总超时
fail_count=0

check_timeout() {
    local now
    now="$(date +%s)"
    if [ $((now - START_TIME)) -ge "${MAX_TIMEOUT}" ]; then
        echo "登录已超时（超过 15 分钟）。之后可随时执行 ./install.sh --qr 或 ./netease_login.sh 重新扫码"
        exit 0
    fi
}

# 3. 外层循环：二维码刷新循环（总超时 15 分钟）
while true; do
    check_timeout

    # a. POST login 拿 unikey + qr_ascii
    login_resp="$(curl -s --max-time 15 -X POST "${MUSICBOX_URL}/api/v1/auth/login" || true)"
    login_ok="$(echo "${login_resp}" | jq -r '.ok // false' 2>/dev/null || echo "false")"
    unikey="$(echo "${login_resp}" | jq -r '.data.unikey // empty' 2>/dev/null || true)"
    qr_ascii="$(echo "${login_resp}" | jq -r '.data.qr_ascii // empty' 2>/dev/null || true)"

    if [ "${login_ok}" != "true" ] || [ -z "${unikey}" ] || [ -z "${qr_ascii}" ]; then
        fail_count=$((fail_count + 1))
        if [ "${fail_count}" -ge 5 ]; then
            echo "网络请求连续失败，登录流程终止。之后可随时执行 ./install.sh --qr 或 ./netease_login.sh 重新扫码"
            exit 0
        fi
        sleep 3
        continue
    fi
    fail_count=0

    echo "------------------------------------------------------------"
    echo "${qr_ascii}"
    echo "请用网易云音乐 App 扫码登录（二维码约 3 分钟有效，过期会自动刷新；不想现在登录可按 Ctrl+C 跳过）"
    echo "------------------------------------------------------------"

    # b. 内层轮询：每 3 秒 GET check
    last_code=""
    while true; do
        sleep 3
        check_timeout

        check_resp="$(curl -s --max-time 10 "${MUSICBOX_URL}/api/v1/auth/login/check?unikey=${unikey}" || true)"
        check_ok="$(echo "${check_resp}" | jq -r '.ok // false' 2>/dev/null || echo "false")"
        code="$(echo "${check_resp}" | jq -r '.data.code // empty' 2>/dev/null || true)"

        if [ "${check_ok}" != "true" ] || [ -z "${code}" ]; then
            fail_count=$((fail_count + 1))
            if [ "${fail_count}" -ge 5 ]; then
                echo "网络请求连续失败，登录流程终止。之后可随时执行 ./install.sh --qr 或 ./netease_login.sh 重新扫码"
                exit 0
            fi
            continue
        fi
        fail_count=0

        case "${code}" in
            801)
                # 等待扫码：仅状态变化时打印
                if [ "${last_code}" != "801" ]; then
                    echo "等待扫码中..."
                    last_code="801"
                fi
                ;;
            802)
                # 已扫码待手机确认
                if [ "${last_code}" != "802" ]; then
                    echo "✅ 已扫码，请在手机上点击确认登录"
                    last_code="802"
                fi
                ;;
            803)
                # 登录成功
                nickname=""
                for (( i=1; i<=3; i++ )); do
                    status_resp="$(curl -s --max-time 5 "${MUSICBOX_URL}/api/v1/auth/status" || true)"
                    nickname="$(echo "${status_resp}" | jq -r '.data.nickname // empty' 2>/dev/null || true)"
                    if [ -n "${nickname}" ]; then
                        break
                    fi
                    [ "${i}" -lt 3 ] && sleep 2
                done
                if [ -z "${nickname}" ]; then
                    nickname="$(echo "${status_resp}" | jq -r '.data.user_id // empty' 2>/dev/null || true)"
                fi
                if [ -z "${nickname}" ]; then
                    nickname="已登录用户"
                fi
                echo "✅ 网易云登录成功：${nickname}"
                exit 0
                ;;
            800)
                # 二维码已过期，跳出内层循环回外层刷新
                echo "二维码已过期，自动刷新..."
                break
                ;;
            *)
                ;;
        esac
    done
done
