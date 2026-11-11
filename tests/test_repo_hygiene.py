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
    r"/home/[a-z]",                                                     # personal paths
    # macOS homes are /Users/<name>/...; a lone /Users/<word> is an API path (Jellyfin).
    r"/Users/[A-Za-z][\w.-]*/",
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


@pytest.mark.parametrize("text,flagged", [
    ("/Users/" + "alice/projects/orama", True),
    ("POST /Users/AuthenticateByName", False),
    ("/Users/Me", False),
])
def test_macos_home_rule(text, flagged):
    pattern = re.compile(next(p for p in FORBIDDEN if p.startswith("/Users")))
    assert bool(pattern.search(text)) is flagged


# The unused Node/Express demo server (hardcoded passwords, an always-valid session token) was
# removed in the 2026-11-11 review. Orama is the Python app in src/ only; nothing may bring a
# second, unreviewed web server back to the repository root.
DEMO_SERVER_PATHS = ("server.ts", "package.json", "package-lock.json", "tsconfig.json",
                     "metadata.json", "views/", "public/")


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True
    ).stdout.decode()
    return [rel for rel in out.split("\0") if rel]


def demo_server_leftovers(tracked):
    """Tracked paths that belong to the removed Node demo server."""
    return sorted(rel for rel in tracked
                  if any(rel == p or (p.endswith("/") and rel.startswith(p))
                         for p in DEMO_SERVER_PATHS))


def test_node_demo_server_stays_removed():
    assert demo_server_leftovers(_tracked_files()) == [], (
        "the insecure Node demo server must not come back; the web app lives in src/orama/web")
    for name in DEMO_SERVER_PATHS:
        assert not (REPO_ROOT / name.rstrip("/")).exists(), name


def test_demo_server_check_sees_files_and_folders():
    tracked = ["server.ts", "views/home.ejs", "public/orama.js", "package.json",
               "src/orama/web/static/orama.js", "docs/public/notes.md", "tests/web/views.py"]
    assert demo_server_leftovers(tracked) == [
        "package.json", "public/orama.js", "server.ts", "views/home.ejs"]


# The owner is the author of every commit (history rewritten on 2026-11-11). AI tools and
# people working on the repo commit under this identity and add a Co-Authored-By line; a
# personal e-mail address or a tool's own name must never enter the public history again.
OWNER_NAME = "DarkAngelGR"
OWNER_EMAIL = "43799929+DarkAngelGR@users.noreply.github.com"
GITHUB_WEB_COMMITTER = "noreply@github.com"  # GitHub's own committer for web edits


def foreign_commits(log_lines):
    """Commits whose author or committer is not the owner (GitHub may be the committer)."""
    bad = []
    for line in log_lines:
        commit, author_name, author_email, committer_email = line.split("\x00")
        if (author_name, author_email) != (OWNER_NAME, OWNER_EMAIL) or committer_email not in (
                OWNER_EMAIL, GITHUB_WEB_COMMITTER):
            bad.append(commit[:7])
    return bad


def test_every_commit_is_authored_by_the_owner():
    log = subprocess.run(
        ["git", "log", "--format=%H%x00%an%x00%ae%x00%ce"], cwd=REPO_ROOT, capture_output=True,
        text=True, check=False)
    if log.returncode != 0:  # not a git checkout (an unpacked archive)
        pytest.skip("no git history here")
    assert foreign_commits(log.stdout.splitlines()) == [], (
        "commits must be authored as the owner; see AGENTS.md (git config user.name/user.email)")


def test_author_check_sees_other_identities():
    owner = "\x00".join(["a" * 40, OWNER_NAME, OWNER_EMAIL, OWNER_EMAIL])
    web = "\x00".join(["b" * 40, OWNER_NAME, OWNER_EMAIL, GITHUB_WEB_COMMITTER])
    tool = "\x00".join(["c" * 40, "Some Tool", "noreply@example.invalid", OWNER_EMAIL])
    other_committer = "\x00".join(["d" * 40, OWNER_NAME, OWNER_EMAIL, "noreply@example.invalid"])
    wrong_name = "\x00".join(["e" * 40, "Someone", OWNER_EMAIL, OWNER_EMAIL])
    assert foreign_commits([owner, web, tool, other_committer, wrong_name]) == [
        "c" * 7, "d" * 7, "e" * 7]
