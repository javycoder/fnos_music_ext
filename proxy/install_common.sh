#!/usr/bin/env bash
# Shared installation-only functions. Source after BASE_DIR and log_* definitions.
installation_lock() {
    # flock parent holds this lock; --close prevents systemd/children inheriting it.
    # A chained install -> extend keeps the parent alive. Services never use it.
    case " ${*} " in *' --help '*|*' -h '*|*' --qr '*) return 0 ;; esac
    if [ "${FNMUSIC_INSTALL_LOCK_HELD:-0}" != 1 ]; then
        # Root creates one stable lock inode; retain the caller's uid/HOME/env.
        sudo /usr/bin/python3 "${BASE_DIR}/proxy/takeover.py" prepare-install-lock || return 1
        exec flock --exclusive --nonblock --close /run/fnmusic-ext-install/operation.lock \
            env FNMUSIC_INSTALL_LOCK_HELD=1 /bin/bash "$0" "$@"
    fi
}

check_proxy_unit_owner() {
    local file="/etc/systemd/system/fnmusic-ext.service" wd
    if [ -f "${file}" ]; then
        wd="$(systemctl show fnmusic-ext.service -p WorkingDirectory --value)" || return 1
        if [ "${wd}" != "${BASE_DIR}" ]; then
            log_err "代理 unit 属于其他目录；拒绝停止或覆盖。请先从原目录还原。"
            return 1
        fi
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
    if [ "${owner}" != "${BASE_DIR}" ]; then
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
    grep -Fqx "WorkingDirectory=${BASE_DIR}/${unit#fnmusic-}-service" "${file}"
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
