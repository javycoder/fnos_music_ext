# fnmusic-sources 统一镜像：musicdl + musicbox + lxmusic + webui 四进程（supervisor 管理，按需启动）
# 基础镜像引用可由 build args 覆盖（默认官方源；国内网络自动回退见根目录 ensure_base_image.sh）
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR="https://mirrors.tuna.tsinghua.edu.cn"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    XDG_DATA_HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    XDG_CONFIG_HOME=/data/config \
    LX_DATA_DIR=/data/lxmusic \
    WEBUI_REPO_DIR=/repo

WORKDIR /srv

# nodejs（洛雪自定义源 Node 沙箱，bookworm 自带 18.x）/ ffmpeg（musicdl 转封装）/ supervisor
# apt 源默认国内镜像（与 pip 同策略，APT_MIRROR 可覆盖），仅在本构建层内临时替换：
# apt update 失败自动回退官方 deb.debian.org 重试；安装完成后恢复官方源，宿主机与最终镜像不受影响
RUN set -eu; \
    swap_apt_host() { \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "${f}" ] && sed -i -E "s#${1}#${2}#g" "${f}" || true; \
        done; \
    }; \
    if [ -n "${APT_MIRROR}" ]; then swap_apt_host 'https?://deb\.debian\.org' "${APT_MIRROR}"; fi; \
    if ! apt-get update -o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15; then \
        echo "[WARN] apt update 失败，回退官方源 deb.debian.org 重试..." >&2; \
        rm -rf /var/lib/apt/lists/*; \
        if [ -n "${APT_MIRROR}" ]; then swap_apt_host "${APT_MIRROR}" 'http://deb.debian.org'; fi; \
        apt-get update; \
    fi; \
    apt-get install -y --no-install-recommends nodejs ffmpeg supervisor ca-certificates; \
    if [ -n "${APT_MIRROR}" ]; then swap_apt_host "${APT_MIRROR}" 'http://deb.debian.org'; fi; \
    rm -rf /var/lib/apt/lists/*

# 四个服务的 Python 依赖并集（各自 requirements 独立演进，镜像统一安装）
COPY musicdl-service/requirements.txt /srv/req.musicdl
COPY musicbox-service/requirements.txt /srv/req.musicbox
COPY lxmusic-service/requirements.txt /srv/req.lxmusic
COPY webui-service/requirements.txt /srv/req.webui
RUN pip install --no-cache-dir -r /srv/req.musicdl -r /srv/req.musicbox -r /srv/req.lxmusic -r /srv/req.webui \
    && rm -f /srv/req.*

RUN useradd -m -u 1000 appuser \
    && mkdir -p /data/cache /data/config /data/lxmusic \
    && chown -R appuser:appuser /data

# --chown+chmod：镜像权限不依赖构建上下文的 umask/属主
COPY --chown=appuser:appuser musicdl-service/app.py musicdl-service/hardening.py /srv/musicdl-service/
COPY --chown=appuser:appuser musicbox-service/app.py musicbox-service/runner.py musicbox-service/netease_ext.py /srv/musicbox-service/
COPY --chown=appuser:appuser lxmusic-service/app.py lxmusic-service/source_runtime.py lxmusic-service/verify_source.py /srv/lxmusic-service/
COPY --chown=appuser:appuser lxmusic-service/js/bridge.js /srv/lxmusic-service/js/bridge.js
COPY --chown=appuser:appuser webui-service/app.py /srv/webui-service/
COPY --chown=appuser:appuser webui-service/static/ /srv/webui-service/static/
RUN find /srv -name "*.py" -exec chmod 0644 {} + \
    && chmod 0755 /srv/lxmusic-service/js/bridge.js \
    && mkdir -p /repo \
    && chmod 0755 /repo /data

COPY container/supervisord.conf /etc/supervisor/supervisord.conf
COPY container/env_flag.sh container/entrypoint.sh container/healthcheck.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/env_flag.sh /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh

USER appuser
EXPOSE 8001 8002 8003 8004

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
