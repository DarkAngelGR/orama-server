# Orama Agent: the only container with the Docker socket. Build from the repository root:
#   docker build -f docker/agent.Dockerfile -t ghcr.io/darkangelgr/orama-agent:<version> .

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
# Docker CLI and compose plugin from Docker's official apt repository, pinned.
# apt fetches over HTTPS (ca-certificates ships in the base image).
ARG DOCKER_CLI_VERSION=5:29.9.2-1~debian.12~bookworm
ARG COMPOSE_PLUGIN_VERSION=5.7.0-1~debian.12~bookworm
# Docker's published apt signing key fingerprint; the build fails if the download differs.
ARG DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
COPY docker/check_key_fingerprint.py /tmp/check_key_fingerprint.py
RUN set -eux; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    install -m 0755 -d /etc/apt/keyrings; \
    python -c "import urllib.request; urllib.request.urlretrieve('https://download.docker.com/linux/debian/gpg', '/tmp/docker.asc')"; \
    python /tmp/check_key_fingerprint.py /tmp/docker.asc "${DOCKER_KEY_FINGERPRINT}"; \
    install -m 0644 /tmp/docker.asc /etc/apt/keyrings/docker.asc; \
    rm /tmp/docker.asc /tmp/check_key_fingerprint.py; \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      "docker-ce-cli=${DOCKER_CLI_VERSION}" "docker-compose-plugin=${COMPOSE_PLUGIN_VERSION}"; \
    rm -rf /var/lib/apt/lists/*; \
    docker --version; docker compose version
COPY --from=build /dist /tmp/dist
RUN pip install --require-hashes -r /tmp/dist/requirements.txt \
 && pip install --no-deps /tmp/dist/*.whl \
 && rm -rf /tmp/dist
# Runs as root: it chowns app folders and talks to the Docker socket. It has no network.
CMD ["python", "-m", "orama_agent"]
