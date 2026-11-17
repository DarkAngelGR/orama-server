# Hermes control app & conversational voice assistant for Orama.
# Build from the repository root:
#   docker build -f docker/hermes.Dockerfile -t ghcr.io/darkangelgr/hermes:<version> .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

ARG INSTALL_LOCAL_TTS=true
RUN pip install --no-cache-dir \
    fastapi==0.163.1 \
    uvicorn==0.54.0 \
    httpx==0.28.1 \
    pydantic==2.14.3 \
 && if [ "$INSTALL_LOCAL_TTS" = "true" ]; then pip install --no-cache-dir piper-tts==1.8.0; fi \
 && useradd --system --uid 10001 hermes

COPY src/hermes ./hermes
RUN mkdir -p /data /voices && chown 10001:10001 /data /voices

USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"]
CMD ["uvicorn", "hermes.app:app", "--host", "0.0.0.0", "--port", "8787", "--no-proxy-headers"]
