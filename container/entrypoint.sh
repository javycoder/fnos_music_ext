#!/bin/sh
# fnmusic-sources 容器入口：supervisor 起来后按 /repo/.env 只拉起所选音源（+WebUI）。
# 全部程序 autostart=false（supervisord.conf），实现按需加载：
# 未启用的音源进程不驻留内存；容器重启后本脚本按 .env 自愈恢复同样的进程集。
set -eu

SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

log() { echo "[entrypoint] $*"; }

export_source_env

# 确保 lxserver 数据目录与初始配置就绪
init_lxserver_config() {
    _lx_data="${DATA_PATH:-/data/lxserver}"
    mkdir -p "${_lx_data}/backups" "${_lx_data}/users/source/_open"
    _cfg_file="${_lx_data}/config.js"
    if [ ! -f "${_cfg_file}" ]; then
        # 默认密码从环境变量获取，或缺省为 123456
        _admin_pw="${LXSERVER_ADMIN_PASSWORD:-123456}"
        cat <<EOF > "${_cfg_file}"
module.exports = {
  serverName: "fnmusic-lxserver",
  bindIP: "127.0.0.1",
  port: 8005,
  frontend: {
    password: "${_admin_pw}"
  },
  player: {
    enableAuth: false
  },
  subsonic: {
    enable: false
  },
  webdav: {
    enable: false
  },
  system: {
    allowUnsafeVM: true
  }
};
EOF
        log "已初始化 lxserver 配置: ${_cfg_file}"
    fi

    # 历史数据无损迁移：从旧 /data/lxmusic 迁移用户自定义源脚本到 lxserver
    _old_lx_data="${LX_DATA_DIR:-/data/lxmusic}"
    if [ -d "${_old_lx_data}/uploads" ]; then
        for _f in "${_old_lx_data}/uploads"/*.js; do
            if [ -f "${_f}" ]; then
                _base="$(basename "${_f}")"
                if [ ! -f "${_lx_data}/users/source/_open/${_base}" ]; then
                    cp -a "${_f}" "${_lx_data}/users/source/_open/"
                    log "已从旧版本迁移自定义源脚本: ${_base}"
                fi
            fi
        done
    fi
    if [ -f "${_old_lx_data}/source.js" ] && [ ! -f "${_lx_data}/users/source/_open/migrated_source.js" ]; then
        if [ ! "$(ls -A "${_lx_data}/users/source/_open" 2>/dev/null)" ]; then
            cp -a "${_old_lx_data}/source.js" "${_lx_data}/users/source/_open/migrated_source.js"
            log "已迁移旧版本激活的自定义源 source.js"
        fi
    fi
}

init_lxserver_config

supervisord -c "$SUP_CONF" &
SUP_PID=$!

# 等 supervisor 控制套接字就绪（最多 10s）。
_i=0
until supervisorctl -c "$SUP_CONF" pid >/dev/null 2>&1; do
    _i=$((_i + 1))
    if [ "$_i" -ge 100 ]; then
        log "supervisor 套接字未就绪，继续等待（进程状态将由 healthcheck 反映）"
        break
    fi
    sleep 0.1
done

start_prog() {
    if supervisorctl -c "$SUP_CONF" start "$1" >/dev/null 2>&1; then
        log "started $1"
    else
        log "WARN: start $1 失败（supervisorctl status 查看原因）"
    fi
}

# 三选一音源：只启动 .env 选中的那个（多开视为异常，全部忽略只取第一个命中）
if [ "$(env_flag FNMUSIC_MUSICDL_ENABLED)" = "true" ]; then
    start_prog musicdl
elif [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" = "true" ]; then
    start_prog musicbox
elif [ "$(env_flag FNMUSIC_LX_ENABLED)" = "true" ]; then
    start_prog lxserver
    start_prog lxmusic
else
    log "未配置任何音源（FNMUSIC_MUSICDL_ENABLED/FNMUSIC_NETEASE_ENABLED/FNMUSIC_LX_ENABLED 均未启用）"
fi

# WebUI 默认随容器启动（--no-webui 安装会写 FNMUSIC_WEBUI_ENABLED=false）
if [ "$(env_flag FNMUSIC_WEBUI_ENABLED)" = "true" ]; then
    start_prog webui
fi

# 信号转发：docker stop → supervisorctl shutdown → 各程序优雅退出
trap 'supervisorctl -c "$SUP_CONF" shutdown >/dev/null 2>&1' TERM INT
log "fnmusic-sources ready (pid=$SUP_PID)"
wait "$SUP_PID"
