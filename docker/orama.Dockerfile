# Orama control app. Build from the repository root:
#   docker build -f docker/orama.Dockerfile -t ghcr.io/darkangelgr/orama:<version> .

# ---- build: a wheel plus hash-pinned runtime requirements from uv.lock
FROM python:3.12.16-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN pip install uv==0.11.32
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project --no-header -o /dist/requirements.txt \
 && uv build --wheel --out-dir /dist

# ---- runtime
FROM python:3.12.16-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
COPY --from=build /dist /tmp/dist
RUN pip install --require-hashes -r /tmp/dist/requirements.txt \
 && pip install --no-deps /tmp/dist/*.whl \
 && rm -rf /tmp/dist \
 && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin orama
# Compose overrides this with `user: PUID:PGID`; the default is never root.
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"]
CMD ["python", "-m", "orama"]
