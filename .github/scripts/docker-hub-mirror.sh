#!/usr/bin/env bash
# CI only: pull Docker Hub images through Google's public Docker Hub mirror.
#
# Docker Hub allows about 100 anonymous image pulls an hour per address, and the hosted
# runners share addresses. Twice the real-app tests failed because `apply` could not
# download the app images (runs 43107652518 and 43333330345). With a mirror the Docker
# daemon asks mirror.gcr.io first and falls back to Docker Hub by itself, so the images,
# tags and digests stay exactly the pinned ones. Images from ghcr.io are not affected.
set -euo pipefail

MIRROR="https://mirror.gcr.io"
CONFIG=/etc/docker/daemon.json

sudo python3 - "$CONFIG" "$MIRROR" <<'PY'
import json, pathlib, sys
path, mirror = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text() if path.exists() else ""
config = json.loads(text) if text.strip() else {}
config["registry-mirrors"] = [mirror]
path.write_text(json.dumps(config, indent=2) + "\n")
PY
sudo systemctl restart docker
# Show the result in the log; fail if the daemon did not take it.
docker info --format '{{json .RegistryConfig.Mirrors}}' | tee /dev/stderr | grep -q "mirror.gcr.io"
