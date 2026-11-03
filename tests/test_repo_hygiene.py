import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Private IPv4: every branch needs a full 4-octet address, so version strings
# (10.10.7) and section refs (§10.1.3) are not flagged. A full stop after the address
# (end of a sentence) still counts; a dot followed by more of a word or number does not.
PRIVATE_IPV4 = (
    r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\.?\w)"
)

FORBIDDEN = [
    PRIVATE_IPV4,
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


def _ip(*octets):
    return ".".join(str(o) for o in octets)


@pytest.mark.parametrize("text", [_ip(10, 0, 0, 5), _ip(192, 168, 1, 20), _ip(172, 16, 0, 1)])
def test_private_ipv4_pattern_flags_addresses(text):
    assert re.search(PRIVATE_IPV4, f"host {text} up")


@pytest.mark.parametrize("text", [_ip(10, 0, 0, 5), _ip(192, 168, 1, 20), _ip(172, 16, 0, 1)])
def test_private_ipv4_pattern_flags_address_ending_a_sentence(text):
    assert re.search(PRIVATE_IPV4, f"Open it at {text}.")
    assert re.search(PRIVATE_IPV4, f"Open it at {text}.\n")


@pytest.mark.parametrize(
    "text", ["10.10.7", "§10.1.3", "4.0.10.2544", _ip(172, 15, 0, 1), "1.10.0.0.1"]
)
def test_private_ipv4_pattern_ignores_non_addresses(text):
    assert not re.search(PRIVATE_IPV4, f"v {text} x")
