# 基础镜像引用可由 build args 覆盖（默认官方源；国内网络自动回退见根目录 ensure_base_image.sh）
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 appuser

# ffmpeg：部分音源 HLS/转封装需要
# apt 源默认国内镜像（与 pip 同策略，APT_MIRROR 可覆盖），仅在本构建层内临时替换：
# apt update 失败自动回退官方 deb.debian.org 重试；安装完成后恢复官方源，宿主机不受影响
ARG APT_MIRROR="https://mirrors.tuna.tsinghua.edu.cn"
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
    apt-get install -y --no-install-recommends ffmpeg ca-certificates; \
    if [ -n "${APT_MIRROR}" ]; then swap_apt_host "${APT_MIRROR}" 'http://deb.debian.org'; fi; \
    rm -rf /var/lib/apt/lists/*

# Install requirements
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code（--chown+chmod：镜像权限不依赖构建上下文的 umask/属主）
COPY --chown=appuser:appuser app.py /app/app.py
COPY --chown=appuser:appuser hardening.py /app/hardening.py
RUN chmod 0644 /app/*.py

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
