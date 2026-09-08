FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 appuser

# ffmpeg：部分音源 HLS/转封装需要
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
