#!/usr/bin/env bash
# Capture pytest before it reaches Actions, and scan the exact upload directory.
set -euo pipefail
umask 077
case "${ORAMA_E2E_TARGET:-}" in
    ubuntu|debian) ;;
    *) echo "An end-to-end target is required" >&2; exit 1 ;;
esac
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
export ORAMA_E2E_REQUIRED=1
export ORAMA_E2E_SCAN_VALUES="$scratch/secrets.json"
status=0
.venv/bin/python -m pytest tests/e2e/test_e2e.py -v -p no:cacheprovider \
    > "$scratch/pytest.log" 2>&1 || status=$?

if [[ ! -s "$ORAMA_E2E_SCAN_VALUES" ]]; then
    echo "End-to-end proof stopped before its diagnostic output could be verified." >&2
    exit 1
fi

.venv/bin/python - "$scratch/pytest.log" "$status" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

values = json.loads(Path(os.environ["ORAMA_E2E_SCAN_VALUES"]).read_text())
output = Path(sys.argv[1]).read_text(errors="replace")
logs = Path(os.environ["ORAMA_E2E_LOG_DIR"])
for path in [Path(sys.argv[1]), *logs.rglob("*")]:
    if path.is_symlink():
        raise SystemExit("Proof output contains a symbolic link")
    if not path.is_file():
        continue
    text = path.read_text(errors="replace")
    if any(value in text for value in values) or re.search(r"/setup\?code=[A-Za-z0-9_-]+", text):
        raise SystemExit("Proof output failed the secret scan. Output was withheld.")
if sys.argv[2] == "0" and not re.search(r"^=+ 1 passed in ", output, re.MULTILINE):
    raise SystemExit("Expected exactly one passing end-to-end test")
print(output, end="")
PY
# The Ubuntu proof runs as root; only scanned files become readable by the uploader.
if [[ -d "$ORAMA_E2E_LOG_DIR" ]]; then
    chmod -R a+rX -- "$ORAMA_E2E_LOG_DIR"
fi
printf 'artifact_safe=true\n' >> "$GITHUB_OUTPUT"
exit "$status"
