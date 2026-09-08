FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_DATA_HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    XDG_CONFIG_HOME=/data/config

WORKDIR /app

RUN useradd -m -u 1000 appuser \
    && mkdir -p /data /data/cache/netease-musicbox /data/config/netease-musicbox /data/netease-musicbox \
    && chown -R appuser:appuser /data \
    && chmod -R 777 /data

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# --chown+chmod：镜像权限不依赖构建上下文的 umask/属主
COPY --chown=appuser:appuser app.py /app/app.py
COPY --chown=appuser:appuser runner.py /app/runner.py
COPY --chown=appuser:appuser netease_ext.py /app/netease_ext.py
RUN chmod 0644 /app/*.py

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
