# fnmusic-sources 统一镜像：musicdl + musicbox + lxmusic + webui 四进程（supervisor 管理，按需启动）
# 基础镜像引用可由 build args 覆盖（默认官方源；国内网络自动回退见根目录 ensure_base_image.sh）
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/
ARG APT_MIRROR="https://mirrors.tencent.com"
ARG NODE_VERSION=v22.14.0
ARG NODE_DIST_MIRROR="https://nodejs.org/dist"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    XDG_DATA_HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    XDG_CONFIG_HOME=/data/config \
    LX_DATA_DIR=/data/lxmusic \
    WEBUI_REPO_DIR=/repo \
    FNMUSIC_LOG_DIR=/data/logs

WORKDIR /srv

# lxserver v2.2.0 要求 Node >=22.13.0；Debian trixie 自带的 20.x 不够，
# 因此从官方发行包安装固定版本，并在同层立即校验完整版本号。
# ffmpeg（musicdl 转封装）/ supervisor 仍走发行版包。
# apt 源默认国内镜像（与 pip 同策略，APT_MIRROR 可覆盖），仅在本构建层内临时替换。
# 构建容器无法复用"指向本机"的宿主 DNS（Docker 会剔除 127.x 并回退 8.8.8.8，国内不可达），
# 因此不探测、始终把国内可达的公共 DNS 写到最前（写入失败不中断）；apt-get update
# 对 DNS 失败只报 W: 警告且退出码为 0，回退判定看「索引是否真正落地」而非退出码；
# 镜像源与官方源都取不到索引才判失败。安装完成后恢复官方源，宿主机与最终镜像不受影响。
# 失败文案统一用 "ERROR:" 前缀，不得写出带方括号的 ERROR 标记（注释里也不行）：
# BuildKit 失败时按行号回显本文件上下文，该字样会被应用中心弹窗错误摘要误收（#109）。
RUN set -eu; \
    fix_dns() { \
        { \
            printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n'; \
            grep -vE '^[[:space:]]*nameserver[[:space:]]+(127\.|::1|localhost)' /etc/resolv.conf 2>/dev/null || true; \
        } > /tmp/resolv.fnmusic 2>/dev/null || true; \
        cat /tmp/resolv.fnmusic > /etc/resolv.conf 2>/dev/null || true; \
    }; \
    swap_apt_host() { \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "${f}" ] && sed -i -E "s#${1}#${2}#g" "${f}" || true; \
        done; \
    }; \
    apt_update() { apt-get update -o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 || true; }; \
    lists_fetched() { [ -n "$(find /var/lib/apt/lists -name '*Packages*' -print -quit 2>/dev/null || true)" ]; }; \
    fix_dns; \
    if [ -n "${APT_MIRROR}" ]; then swap_apt_host 'https?://deb\.debian\.org' "${APT_MIRROR}"; fi; \
    rm -rf /var/lib/apt/lists/*; \
    apt_update; \
    if ! lists_fetched; then \
        echo "[WARN] apt 镜像源未取到任何索引，回退官方源 deb.debian.org 重试..." >&2; \
        fix_dns; \
        rm -rf /var/lib/apt/lists/*; \
        if [ -n "${APT_MIRROR}" ]; then swap_apt_host "${APT_MIRROR}" 'http://deb.debian.org'; fi; \
        apt_update; \
    fi; \
    if ! lists_fetched; then \
        if ! grep -q '^nameserver 223\.5\.5\.5' /etc/resolv.conf 2>/dev/null; then \
            echo "ERROR: 构建容器备用 DNS 注入未生效（/etc/resolv.conf 不可改写或被覆盖）。" >&2; \
        fi; \
        echo "ERROR: apt 镜像源与官方源均未取到索引：构建容器无法访问 Debian 源。" >&2; \
        echo "ERROR: 若宿主机 /etc/resolv.conf 的 DNS 指向本机（127.x），Docker 容器无法复用，" >&2; \
        echo "ERROR: 可在 Docker daemon.json 配置 \"dns\": [\"223.5.5.5\"] 并重启 Docker 后重试。" >&2; \
        exit 1; \
    fi; \
    apt-get install -y --no-install-recommends ffmpeg supervisor ca-certificates unzip curl xz-utils; \
    if [ -n "${APT_MIRROR}" ]; then swap_apt_host "${APT_MIRROR}" 'http://deb.debian.org'; fi; \
    rm -rf /var/lib/apt/lists/*

# Node 发行包同时校验官方 SHASUMS256；镜像仅改变下载源，不改变被校验的文件名。
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
        amd64) node_arch="x64" ;; \
        arm64) node_arch="arm64" ;; \
        *) echo "ERROR: 不支持的 Node 架构 $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    node_file="node-${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    for mirror in "${NODE_DIST_MIRROR}" "https://npmmirror.com/mirrors/node"; do \
        base="${mirror%/}/${NODE_VERSION}"; \
        if curl -fSL --connect-timeout 15 --max-time 300 "${base}/${node_file}" -o "/tmp/${node_file}" \
            && curl -fSL --connect-timeout 15 --max-time 60 "${base}/SHASUMS256.txt" -o /tmp/SHASUMS256.txt; then \
            break; \
        fi; \
    done; \
    test -s "/tmp/${node_file}" && test -s /tmp/SHASUMS256.txt; \
    (cd /tmp && grep "  ${node_file}\$" SHASUMS256.txt | sha256sum -c -); \
    tar -xJf "/tmp/${node_file}" -C /usr/local --strip-components=1; \
    node -e 'const [a,b]=process.versions.node.split(".").map(Number); if (a < 22 || (a === 22 && b < 13)) { console.error(`Node ${process.versions.node} < 22.13.0`); process.exit(1); }'; \
    node --version; \
    npm --version

# 四个服务的 Python 依赖并集（各自 requirements 独立演进，镜像统一安装）
# 构建容器 DNS 失败自愈同 apt 步骤（resolv.conf 改写不跨 RUN 层持久，需每步自带）；
# 镜像源安装失败自动回退官方 PyPI 重试
COPY musicdl-service/requirements.txt /srv/req.musicdl
COPY lxmusic-service/requirements.txt /srv/req.lxmusic
COPY webui-service/requirements.txt /srv/req.webui
RUN set -eu; \
    fix_dns() { \
        { \
            printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n'; \
            grep -vE '^[[:space:]]*nameserver[[:space:]]+(127\.|::1|localhost)' /etc/resolv.conf 2>/dev/null || true; \
        } > /tmp/resolv.fnmusic 2>/dev/null || true; \
        cat /tmp/resolv.fnmusic > /etc/resolv.conf 2>/dev/null || true; \
    }; \
    fix_dns; \
    pip install --no-cache-dir -r /srv/req.musicdl -r /srv/req.lxmusic -r /srv/req.webui \
    || { echo "[WARN] pip 镜像源（${PIP_INDEX_URL}）安装失败，回退官方 PyPI 重试..." >&2; \
         pip install --no-cache-dir -i https://pypi.org/simple \
            -r /srv/req.musicdl -r /srv/req.lxmusic -r /srv/req.webui; } \
    && rm -f /srv/req.*

RUN groupadd -g 1000 appuser \
    && useradd -m -u 1000 -g 1000 appuser \
    && mkdir -p /data/cache /data/config /data/lxmusic /data/lxserver /data/lxserver-meta /data/logs \
    && chown -R appuser:appuser /data

# lxserver 预编译包引入：构建上下文优先（预置离线 zip）+ 多镜像加速回退链
ARG LXSERVER_MIRROR="https://ghfast.top"
COPY container/lxserver.version /srv/lxserver.version
COPY container/lxserver-artifact/ /srv/lxserver-artifact/
COPY container/ensure_lxserver_native.sh /usr/local/bin/ensure-lxserver-native
RUN set -eu; \
    . /srv/lxserver.version; \
    TARGET_ZIP="/srv/lxserver-artifact/${LXSERVER_ZIP_NAME}"; \
    if [ ! -s "${TARGET_ZIP}" ]; then \
        echo "[INFO] 预置包不存在或为空，通过镜像下载 ${LXSERVER_ZIP_NAME}..."; \
        DOWNLOAD_OK=0; \
        if [ -z "${APT_MIRROR:-}" ]; then \
            _chain="\"\" ${LXSERVER_MIRROR} https://gh-proxy.com"; \
        else \
            _chain="${LXSERVER_MIRROR} https://gh-proxy.com https://github.moeyy.xyz \"\""; \
        fi; \
        for mirror in ${_chain}; do \
            if [ "${mirror}" = "\"\"" ] || [ -z "${mirror}" ]; then \
                URL="https://github.com/XCQ0607/lxserver/releases/download/${LXSERVER_TAG}/${LXSERVER_ZIP_NAME}"; \
            else \
                URL="${mirror}/https://github.com/XCQ0607/lxserver/releases/download/${LXSERVER_TAG}/${LXSERVER_ZIP_NAME}"; \
            fi; \
            echo "[INFO] 尝试从 ${URL} 下载..."; \
            if curl -fSL --connect-timeout 10 --max-time 180 "${URL}" -o "${TARGET_ZIP}"; then \
                if [ -s "${TARGET_ZIP}" ]; then DOWNLOAD_OK=1; break; fi; \
            fi; \
        done; \
        if [ "${DOWNLOAD_OK}" -ne 1 ]; then \
            echo "ERROR: lxserver release zip 下载失败，请预先放置于 container/lxserver-artifact/" >&2; \
            exit 1; \
        fi; \
    fi; \
    ACTUAL_SHA="$(sha256sum "${TARGET_ZIP}" | awk '{print $1}')"; \
    if [ -n "${LXSERVER_SHA256:-}" ] && [ "${ACTUAL_SHA}" != "${LXSERVER_SHA256}" ]; then \
        echo "ERROR: lxserver zip 校验和不符 (${ACTUAL_SHA} != ${LXSERVER_SHA256})" >&2; \
        exit 1; \
    fi; \
    mkdir -p /srv/lxserver; \
    unzip -q -o "${TARGET_ZIP}" -d /srv/lxserver-tmp; \
    if [ -d "/srv/lxserver-tmp/lx-music-sync-server" ]; then \
        cp -a /srv/lxserver-tmp/lx-music-sync-server/. /srv/lxserver/; \
    else \
        cp -a /srv/lxserver-tmp/. /srv/lxserver/; \
    fi; \
    test -f /srv/lxserver/server/server/sources/userApi.js; \
    node -e 'const u=require("/srv/lxserver/server/server/sources/userApi.js"); if (typeof u.loadUserApi !== "function" || typeof u.extractMetadata !== "function") process.exit(1)'; \
    chmod 0755 /usr/local/bin/ensure-lxserver-native; \
    /usr/local/bin/ensure-lxserver-native \
        --artifact-dir /srv/lxserver-artifact \
        --lxserver-dir /srv/lxserver \
        --version-file /srv/lxserver.version; \
    rm -rf /srv/lxserver-tmp /srv/lxserver-artifact; \
    chown -R appuser:appuser /srv/lxserver

# netease-service npm 依赖
COPY --chown=appuser:appuser netease-service/package.json netease-service/package-lock.json /srv/netease-service/
RUN set -eu; \
    cd /srv/netease-service; \
    npm ci --omit=dev --ignore-scripts; \
    chown -R appuser:appuser /srv/netease-service
COPY --chown=appuser:appuser netease-service/service.js netease-service/app.js /srv/netease-service/

# --chown+chmod：镜像权限不依赖构建上下文的 umask/属主
COPY --chown=appuser:appuser musicdl-service/app.py musicdl-service/hardening.py musicdl-service/search_core.py musicdl-service/worker.py /srv/musicdl-service/
# 音质 + 播放编排新模块随本轮一并分发：
#   proxy/quality_policy.py        —— A 组（主智能体）共享七档音质策略
#   lxmusic-service/playback_policy.py —— B 组（洛雪）播放编排
# 两个文件属其他并行分支，本轮合入前文件可能尚不存在：那时 COPY 会失败，
# 这是刻意保留的硬失败——不能删掉 COPY 行绕过，否则容器里缺少模块会在
# 运行期才报 ModuleNotFoundError。集成时请先确认两个文件已落盘。
COPY --chown=appuser:appuser \
    lxmusic-service/app.py \
    lxmusic-service/lxserver_client.py \
    lxmusic-service/managed_client.py \
    lxmusic-service/playback_policy.py \
    lxmusic-service/source_cpu.py \
    lxmusic-service/source_runtime.py \
    lxmusic-service/verify_source.py \
    lxmusic-service/source_worker.cjs \
    lxmusic-service/lx_vm_bridge.cjs \
    /srv/lxmusic-service/
COPY --chown=appuser:appuser webui-service/app.py webui-service/config_jobs.py /srv/webui-service/
COPY --chown=appuser:appuser webui-service/static/ /srv/webui-service/static/
# webui/lxmusic/musicbox 复用 proxy 模块（.env 安全合并 + 统一日志 fnlog），必须随镜像分发
COPY --chown=appuser:appuser proxy/__init__.py proxy/direct_play.py proxy/env_merge.py proxy/fnlog.py proxy/llm_usage.py proxy/media_url.py proxy/netease_legacy.py proxy/publicplaylists.py proxy/quality_policy.py proxy/userstorage.py proxy/version.py /srv/proxy/
COPY --chown=appuser:appuser VERSION RELEASE_CHANNEL /srv/
RUN chmod 0644 /srv/VERSION /srv/RELEASE_CHANNEL \
    && find /srv -type f \( -name "*.py" -o -name "*.cjs" \) -exec chmod 0644 {} + \
    && mkdir -p /repo \
    && chmod 0755 /repo /data

COPY container/supervisord.conf /etc/supervisor/supervisord.conf
COPY container/env_flag.sh container/entrypoint.sh container/healthcheck.sh /usr/local/bin/
# COPY 保留上下文文件 mode：umask 077 检出下 supervisord.conf 会以 600 进镜像，
# appuser 读不了配置导致容器起不来；权限全部显式规范化，不依赖构建上下文
RUN chmod 0755 /usr/local/bin/env_flag.sh /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh \
    && chmod 0644 /etc/supervisor/supervisord.conf

USER appuser
EXPOSE 8001 8002 8003 8004

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
