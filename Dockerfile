FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    TZ=Asia/Shanghai \
    LC_PORT=887 \
    LC_DATA_DIR=/app/data \
    LC_CONFIG=/app/config.yaml

# 动态样式用 ffmpeg 合成动图；NAS 直连 debian 源太慢，换清华镜像
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY fonts ./fonts
COPY templates ./templates
COPY static ./static
COPY config.example.yaml ./config.example.yaml
COPY LICENSE NOTICE ./

RUN mkdir -p /app/data

EXPOSE 887

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('LC_PORT','887'))"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${LC_PORT:-887}"]
