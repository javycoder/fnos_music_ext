#!/usr/bin/env bash
# Shared installation-only functions. Source after BASE_DIR and log_* definitions.
# NOTE: installation_lock()/retire_hung_installers() run BEFORE the caller's
# log_* helpers are defined (install.sh acquires the lock at the very top), so
# they must print with plain echo instead of log_warn/log_err.
INSTALL_LOCK_FILE="${FNMUSIC_INSTALL_LOCK_FILE:-/run/fnmusic-ext-install/operation.lock}"

installation_lock() {
    # flock parent holds this lock; --close prevents systemd/children inheriting it.
    # A chained install -> extend keeps the parent alive. Services never use it.
    case " ${*} " in *' --help '*|*' -h '*|*' --qr '*) return 0 ;; esac
    if [ "${FNMUSIC_INSTALL_LOCK_HELD:-0}" != 1 ]; then
        # Root creates one stable lock inode; retain the caller's uid/HOME/env.
        sudo /usr/bin/python3 "${BASE_DIR}/proxy/takeover.py" prepare-install-lock || return 1
        local attempt
        for attempt in 1 2 3; do
            if flock --exclusive --nonblock --close "${INSTALL_LOCK_FILE}" true 2>/dev/null; then
                # Tiny TOCTOU window vs. a concurrent taker is accepted: the
                # exec'd flock below fails loudly in that rare case (bash exits
                # with flock's non-zero status), same as the previous behavior.
                exec flock --exclusive --nonblock --close "${INSTALL_LOCK_FILE}" \
                    env FNMUSIC_INSTALL_LOCK_HELD=1 /bin/bash "$0" "$@"
            fi
            # Lock is taken: a hung installer from THIS checkout is retired
            # (an interactive wizard stuck on QR login is the common case);
            # foreign holders are reported and abort, never signalled.
            if ! retire_hung_installers; then
                return 1
            fi
            sleep 1
        done
        echo -e "\033[31m[ERROR]\033[0m 安装锁 ${INSTALL_LOCK_FILE} 仍被占用（已重试 3 次）。请确认没有其他安装/扩展/还原正在运行后重试。" >&2
        return 1
    fi
}

retire_hung_installers() {
    # Inspect whoever holds the operation lock and terminate hung installer
    # trees from THIS checkout only. Double guard before any signal:
    #   1) the holder's command line references one of this project's scripts;
    #   2) the command line or its cwd resolves to THIS checkout (BASE_DIR).
    # Anything else (another checkout's copy, an unrelated process, or an
    # invisible root process) is preserved and reported instead.
    # Optional $1 supplies the lock-holders JSON (tests use this to inject).
    local payload holders_raw pid raw cmdline cwd pgid my_pgid killed=0
    if [ -n "${1:-}" ]; then
        payload="$1"
    else
        payload="$(sudo /usr/bin/python3 "${BASE_DIR}/proxy/takeover.py" lock-holders \
            --lock-file "${INSTALL_LOCK_FILE}" 2>/dev/null || true)"
    fi
    # One pid per line for holders with a usable identity.
    holders_raw="$(printf '%s' "${payload}" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for holder in data.get("holders", []):
    if holder.get("pid"):
        print(holder["pid"])
' 2>/dev/null || true)"
    if [ -z "${holders_raw}" ]; then
        echo -e "\033[31m[ERROR]\033[0m 安装锁被占用，但无法识别持有进程（可能是其他用户/根进程持有）。"
        echo -e "\033[31m[ERROR]\033[0m 请确认没有其他安装/扩展/还原正在运行，或手动排查: sudo fuser -v ${INSTALL_LOCK_FILE}" >&2
        return 1
    fi
    my_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ' || true)"
    for pid in ${holders_raw}; do
        raw="$(cat "/proc/${pid}/cmdline" 2>/dev/null | tr '\0' ' ' || true)"
        cmdline="${raw}"
        cwd="$(readlink -f -- "/proc/${pid}/cwd" 2>/dev/null || true)"
        pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d ' ' || true)"
        case "${cmdline}" in
            *install.sh*|*extend.sh*|*restore.sh*|*netease_login.sh*|*ensure_base_image.sh*)
                if [ -n "${cmdline}" ] && [[ "${cmdline}" == *"$(readlink -f -- "${BASE_DIR}")"* \
                    || "${cwd}" = "$(readlink -f -- "${BASE_DIR}")" ]]; then
                    # Never signal our own process group (suicide guard).
                    if [ -n "${pgid}" ] && [ "${pgid}" = "${my_pgid}" ]; then
                        echo -e "\033[33m[WARN]\033[0m 锁持有进程与当前脚本同组，跳过终止 (PID ${pid})。"
                        return 1
                    fi
                    echo -e "\033[33m[WARN]\033[0m 终止挂起的本仓库安装进程 (PID ${pid}: ${cmdline})"
                    if [ -n "${pgid}" ]; then
                        kill -TERM -- "-${pgid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null || true
                    else
                        kill -TERM "${pid}" 2>/dev/null || true
                    fi
                    killed=1
                else
                    echo -e "\033[31m[ERROR]\033[0m 安装锁由另一份仓库副本持有，保留不终止 (PID ${pid}: ${cmdline:-未知})。"
                    echo -e "\033[31m[ERROR]\033[0m 请到该副本目录处理，或等其结束后重试。" >&2
                    return 1
                fi
                ;;
            *)
                echo -e "\033[31m[ERROR]\033[0m 安装锁持有进程不是本工具脚本，保留不终止 (PID ${pid}: ${cmdline:-未知})。" >&2
                return 1
                ;;
        esac
    done
    if [ "${killed}" -eq 1 ]; then
        # Give the terminated tree a moment to release the lock fd.
        local waited=0
        while [ "${waited}" -lt 10 ]; do
            flock --exclusive --nonblock --close "${INSTALL_LOCK_FILE}" true 2>/dev/null && return 0
            sleep 0.5
            waited=$((waited + 1))
        done
        # Still holding: escalate to SIGKILL for the same guarded targets only.
        for pid in ${holders_raw}; do
            pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d ' ' || true)"
            [ -n "${pgid}" ] && [ "${pgid}" != "${my_pgid}" ] \
                && kill -KILL -- "-${pgid}" 2>/dev/null || true
        done
        sleep 1
        flock --exclusive --nonblock --close "${INSTALL_LOCK_FILE}" true 2>/dev/null && return 0
        echo -e "\033[31m[ERROR]\033[0m 已终止挂起进程但安装锁仍未释放，请手动排查: sudo fuser -v ${INSTALL_LOCK_FILE}" >&2
        return 1
    fi
    return 1
}

same_dir() {
    # One checkout is reachable as /home/admin/... and /vol2/home/admin/... on
    # fnOS: compare canonical paths so a symlinked spelling of the SAME directory
    # is never mistaken for a foreign deployment (which would block install/restore).
    local left right
    left="$(readlink -f -- "${1:-}" 2>/dev/null || true)"
    right="$(readlink -f -- "${2:-}" 2>/dev/null || true)"
    [ -n "${left}" ] && [ "${left}" = "${right}" ]
}

unit_working_dir() {
    # WorkingDirectory as recorded in a unit file (no systemd interaction needed).
    sed -n 's/^[[:space:]]*WorkingDirectory=//p' "$1" 2>/dev/null | head -n1
}

check_proxy_unit_owner() {
    # Unit ownership is its WorkingDirectory. Accept the caller's "$@" so
    # --adopt (explicit migration) can bypass, mirroring check_deployment_owner.
    local file="/etc/systemd/system/fnmusic-ext.service" wd
    [ -f "${file}" ] || return 0
    wd="$(systemctl show fnmusic-ext.service -p WorkingDirectory --value 2>/dev/null || true)"
    if [ -z "${wd}" ]; then
        # systemctl show can fail or lag behind a manual unit edit; the unit
        # file itself is the ground truth for what would be overwritten.
        wd="$(unit_working_dir "${file}")"
    fi
    if [ -z "${wd}" ]; then
        log_err "无法读取 ${file} 的 WorkingDirectory，拒绝盲目接管该 unit。"
        log_err "请检查该 unit 文件后重试，或手动清理："
        log_err "  sudo systemctl disable --now fnmusic-ext.service && sudo rm '${file}'"
        return 1
    fi
    if same_dir "${wd}" "${BASE_DIR}"; then
        return 0
    fi
    if [ ! -d "${wd}" ]; then
        # Same semantics as takeover.py deployment_conflict for the registry:
        # a deleted checkout cannot be protected, this one may adopt it.
        log_warn "代理 unit 属于已不存在的目录 ${wd}，视为废弃部署，本次将直接接管。"
        return 0
    fi
    case " ${*} " in
        *' --adopt '*)
            log_warn "代理 unit 属于目录 ${wd}，按 --adopt 迁移部署到当前目录 ${BASE_DIR}。"
            return 0
            ;;
    esac
    log_err "代理 unit 属于其他目录 ${wd}（当前目录 ${BASE_DIR}），拒绝停止或覆盖。"
    log_err "  1) 到原目录 ${wd} 执行 ./restore.sh 释放部署；"
    log_err "  2) 确认要把部署迁移到当前目录：追加 --adopt 重新运行；"
    log_err "  3) 手动清理：sudo systemctl disable --now fnmusic-ext.service && sudo rm /etc/systemd/system/fnmusic-ext.service"
    return 1
}

check_deployment_owner() {
    # Machine-wide deployment registry: the unit name, container names and
    # installer lock are global; a second checkout must not silently become
    # the deployment. Pass the script's own "$@" so --adopt (explicit
    # migration) can skip the check. Run AFTER log_* helpers are defined.
    case " ${*} " in *' --adopt '*) return 0 ;; esac
    local out
    if ! out="$(/usr/bin/python3 "${BASE_DIR}/proxy/takeover.py" deployment-check --base "${BASE_DIR}" 2>/dev/null)"; then
        log_err "本机的 fnmusic-ext 部署属于另一目录: ${out}"
        log_err "请在原部署目录维护；若原目录已废弃，请先在其中执行 ./restore.sh 释放部署。"
        log_err "确认要把部署迁移到当前目录，请追加 --adopt 重新运行。"
        return 1
    fi
}

takeover() {
    sudo /usr/bin/python3 "${BASE_DIR}/proxy/takeover.py" "$@"
}

wait_http() {
    local url="$1" tries="${2:-60}" delay="${3:-2}"
    # timeout bounds the whole loop, including slow responses, not only sleeps.
    timeout --foreground "$((tries * delay))s" /bin/bash -c '
        while ! curl --fail --silent --max-time 4 "$1" >/dev/null 2>&1; do
            sleep "$2"
        done
    ' wait-http "${url}" "${delay}"
}

reclaim_container() {
    # Compose can reconcile its own containers without destructive rm -f.
    local name="$1" owner=""
    if ! run_docker container inspect "${name}" >/dev/null 2>&1; then
        return 0
    fi
    owner="$(run_docker container inspect "${name}" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
    if ! same_dir "${owner}" "${BASE_DIR}"; then
        log_err "容器 ${name} 不属于当前目录；保留并拒绝接管。请先解决名称/端口冲突。"
        return 1
    fi
}

remove_owned_container() {
    local name="$1"
    if ! run_docker container inspect "${name}" >/dev/null 2>&1; then
        return 0
    fi
    reclaim_container "${name}" || return 1
    run_docker rm -f "${name}"
}

owned_source_unit() {
    local unit="$1" file="/etc/systemd/system/${1}.service"
    [ -f "${file}" ] || return 1
    same_dir "$(unit_working_dir "${file}")" "${BASE_DIR}/${unit#fnmusic-}-service"
}

stop_owned_source_unit() {
    local unit="$1"
    if owned_source_unit "${unit}"; then
        sudo systemctl disable --now "${unit}.service"
    elif systemctl is-active --quiet "${unit}.service"; then
        log_err "宿主机 unit ${unit} 不属于当前目录；保留。"
        return 1
    fi
}
