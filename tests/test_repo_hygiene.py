import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = [
    r"(?<![\w.§])(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",  # private IPv4 (not section refs like §10.1.3)
    r"/home/[a-z]", r"/Users/[A-Za-z]",                                  # personal paths
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b[A-Za-z0-9+/]{43}=",                                            # WireGuard key shape
    r"@gmail\.com",
]


def scan_tracked_files(patterns, exclude=frozenset()):
    """Return [(path, line_no, pattern)] for every tracked text line matching a pattern."""
    compiled = [(p, re.compile(p)) for p in patterns]
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True
    ).stdout.decode()
    hits = []
    for rel in filter(None, out.split("\0")):
        if rel in exclude:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for raw, rx in compiled:
                if rx.search(line):
                    hits.append((rel, line_no, raw))
    return hits


def test_no_secrets_ips_or_personal_paths():
    hits = scan_tracked_files(FORBIDDEN, exclude={"tests/test_repo_hygiene.py"})
    assert hits == []
