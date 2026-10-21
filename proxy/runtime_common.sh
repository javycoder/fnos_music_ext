#!/usr/bin/env bash
# Shared host-runtime discovery for installers. Sourcing this file has no side effects.

runtime_python_ok() {
    local bin="$1"
    local resolved
    if [[ "${bin}" == */* ]]; then
        [ -x "${bin}" ] || return 1
        resolved="${bin}"
    else
        resolved="$(command -v "${bin}" 2>/dev/null || true)"
        [ -n "${resolved}" ] && [ -x "${resolved}" ] || return 1
    fi
    "${resolved}" - <<'PY' >/dev/null 2>&1
import ensurepip, sqlite3, ssl, sys, venv
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

runtime_python_version() {
    local bin="$1"
    local resolved
    if [[ "${bin}" == */* ]]; then
        resolved="${bin}"
    else
        resolved="$(command -v "${bin}" 2>/dev/null || true)"
    fi
    [ -n "${resolved}" ] && "${resolved}" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo unknown
}

choose_python_bin() {
    local candidate
    FNMUSIC_SELECTED_PYTHON=""
    FNMUSIC_PYTHON_SOURCE=""
    if [ -n "${FNMUSIC_PYTHON_BIN:-}" ]; then
        if ! runtime_python_ok "${FNMUSIC_PYTHON_BIN}"; then
            return 1
        fi
        if [[ "${FNMUSIC_PYTHON_BIN}" == */* ]]; then
            FNMUSIC_SELECTED_PYTHON="${FNMUSIC_PYTHON_BIN}"
        else
            FNMUSIC_SELECTED_PYTHON="$(command -v "${FNMUSIC_PYTHON_BIN}")"
        fi
        FNMUSIC_PYTHON_SOURCE="explicit"
        return 0
    fi
    local fnos_candidates=(
        /var/apps/python312/target/bin/python3
        /var/apps/python312/target/bin/python3.12
        /vol1/@appcenter/python312/bin/python3
    )
    for candidate in "${fnos_candidates[@]}"; do
        if runtime_python_ok "${candidate}"; then
            FNMUSIC_SELECTED_PYTHON="${candidate}"
            FNMUSIC_PYTHON_SOURCE="fnOS python312"
            return 0
        fi
    done
    local any_fnos
    for any_fnos in /var/apps/python*/target/bin/python3 /vol1/@appcenter/python*/bin/python3; do
        if [ -x "${any_fnos}" ] && runtime_python_ok "${any_fnos}"; then
            FNMUSIC_SELECTED_PYTHON="${any_fnos}"
            FNMUSIC_PYTHON_SOURCE="fnOS python"
            return 0
        fi
    done
    local sys_candidates=(python3 python3.12 python3.11 python3.13)
    for candidate in "${sys_candidates[@]}"; do
        if runtime_python_ok "${candidate}"; then
            FNMUSIC_SELECTED_PYTHON="$(command -v "${candidate}")"
            FNMUSIC_PYTHON_SOURCE="system python3"
            return 0
        fi
    done
    return 1
}

runtime_node_ok() {
    local bin="$1"
    local resolved
    if [[ "${bin}" == */* ]]; then
        [ -x "${bin}" ] || return 1
        resolved="${bin}"
    else
        resolved="$(command -v "${bin}" 2>/dev/null || true)"
        [ -n "${resolved}" ] && [ -x "${resolved}" ] || return 1
    fi
    local major
    major="$("${resolved}" -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)"
    case "${major}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "${major}" -ge 20 ]
}

choose_node_bin() {
    local candidate
    FNMUSIC_SELECTED_NODE=""
    FNMUSIC_NODE_SOURCE=""
    if [ -n "${FNMUSIC_NODE_BIN:-}" ]; then
        if ! runtime_node_ok "${FNMUSIC_NODE_BIN}"; then
            return 1
        fi
        if [[ "${FNMUSIC_NODE_BIN}" == */* ]]; then
            FNMUSIC_SELECTED_NODE="${FNMUSIC_NODE_BIN}"
        else
            FNMUSIC_SELECTED_NODE="$(command -v "${FNMUSIC_NODE_BIN}")"
        fi
        FNMUSIC_NODE_SOURCE="explicit"
        return 0
    fi
    local fnos_node22=(
        /var/apps/nodejs_v22/target/bin/node
        /vol1/@appcenter/nodejs_v22/bin/node
    )
    for candidate in "${fnos_node22[@]}"; do
        if runtime_node_ok "${candidate}"; then
            FNMUSIC_SELECTED_NODE="${candidate}"
            FNMUSIC_NODE_SOURCE="fnOS nodejs_v22"
            return 0
        fi
    done
    local any_fnos_node
    for any_fnos_node in /var/apps/nodejs_v*/target/bin/node /vol1/@appcenter/nodejs_v*/bin/node; do
        if [ -x "${any_fnos_node}" ] && runtime_node_ok "${any_fnos_node}"; then
            FNMUSIC_SELECTED_NODE="${any_fnos_node}"
            case "${any_fnos_node}" in
                *nodejs_v22*) FNMUSIC_NODE_SOURCE="fnOS nodejs_v22" ;;
                *) FNMUSIC_NODE_SOURCE="fnOS node" ;;
            esac
            return 0
        fi
    done
    if runtime_node_ok "node"; then
        FNMUSIC_SELECTED_NODE="$(command -v node)"
        FNMUSIC_NODE_SOURCE="system node"
        return 0
    fi
    return 1
}

runtime_tool_path() {
    local explicit="$1"
    local tool="$2"
    if [ -n "${explicit}" ]; then
        if [[ "${explicit}" == */* ]]; then
            [ -x "${explicit}" ] && printf '%s\n' "${explicit}" && return 0
        else
            command -v "${explicit}" 2>/dev/null && return 0
        fi
        return 1
    fi
    local candidate
    for candidate in "$(command -v "${tool}" 2>/dev/null || true)" \
                     "/usr/trim/lib/mediasrv/${tool}" \
                     "/usr/bin/${tool}"; do
        if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

choose_media_bins() {
    FNMUSIC_SELECTED_FFMPEG="$(runtime_tool_path "${FNMUSIC_FFMPEG_BIN:-}" ffmpeg || true)"
    FNMUSIC_SELECTED_FFPROBE="$(runtime_tool_path "${FNMUSIC_FFPROBE_BIN:-}" ffprobe || true)"
    [ -n "${FNMUSIC_SELECTED_FFMPEG}" ]
}

deps_requirements_digest() {
    local file digest=""
    for file in "$@"; do
        digest="${digest}$(sha256sum "${file}" 2>/dev/null | awk '{print $1}')"
    done
    printf '%s' "${digest}" | sha256sum | awk '{print $1}'
}

deps_python_identity() {
    "${1}" - <<'PY' 2>/dev/null || echo unknown
import os, sys
print(os.path.realpath(sys.executable), f"{sys.version_info[0]}.{sys.version_info[1]}")
PY
}

deps_expected_record() {
    local venv="$1" revision="$2" identity="$3" digest="$4"
    printf 'revision=%s\ninterpreter=%s\nrequirements=%s\n' \
        "${revision}" "${identity}" "${digest}"
}

deps_imports_ok() {
    local python="$1"; shift
    "${python}" - "$@" <<'PY' >/dev/null 2>&1
import importlib, sys
for name in sys.argv[1:]:
    importlib.import_module(name)
PY
}

deps_state_valid() {
    local venv="$1" revision="$2" python="$3" digest="$4"
    shift 4
    [ -x "${python}" ] || return 1
    [ -f "${venv}/.fnmusic-deps" ] || return 1
    [ "$(cat "${venv}/.fnmusic-deps" 2>/dev/null)" = "$(deps_expected_record \
        "${venv}" "${revision}" "$(deps_python_identity "${python}")" "${digest}")" ] || return 1
    "${python}" -m pip check >/dev/null 2>&1 || return 1
    deps_imports_ok "${python}" "$@"
}

deps_write_state() {
    local venv="$1" revision="$2" python="$3" digest="$4"
    printf '%s\n' "$(deps_expected_record "${venv}" "${revision}" \
        "$(deps_python_identity "${python}")" "${digest}")" > "${venv}/.fnmusic-deps"
}
