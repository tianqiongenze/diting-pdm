# syntax=docker/dockerfile:1.7
# 构建上下文 = impl/（不是本目录），因为要同时打包 packages/pdm_signal
#   docker build -t pdm/python-diagnosis:0.1.0 -f services/python-diagnosis/Dockerfile .
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY services/python-diagnosis/requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/_pdm_signal
WORKDIR /app

# 非 root（与 deploy/k8s 的 runAsUser 10001 对齐）
RUN groupadd -g 10001 pdm && useradd -u 10001 -g pdm -s /sbin/nologin pdm

COPY --from=deps /install /usr/local
COPY services/python-diagnosis/app ./app
COPY packages/pdm_signal ./_pdm_signal

USER 10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"

CMD ["granian", "--interface", "asgi", "app.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "warning"]
