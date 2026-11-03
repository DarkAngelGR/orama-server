"""docker/check_key_fingerprint.py: the agent image build trusts Docker's apt key only
through it. Every key and packet here is built at runtime (no key-shaped text in the repo).
"""

import base64
import hashlib
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "check_key_fingerprint.py"
_spec = importlib.util.spec_from_file_location("check_key_fingerprint", SCRIPT)
ckf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ckf)


def armor(data: bytes) -> str:
    b64 = base64.b64encode(data).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "\n".join(
        ["-----BEGIN PGP PUBLIC KEY BLOCK-----", "", *lines, "=abcd",
         "-----END PGP PUBLIC KEY BLOCK-----", ""]
    )


def packet(tag: int, body: bytes) -> bytes:
    """A new-format packet with a one- or five-byte length."""
    if len(body) < 192:
        return bytes([0xC0 | tag, len(body)]) + body
    return bytes([0xC0 | tag, 255]) + len(body).to_bytes(4) + body


def v4_key_body() -> bytes:
    """A v4 public-key packet body: version, time, EdDSA, curve OID, a random point MPI."""
    oid = bytes.fromhex("2b06010401da470f01")
    point = b"\x40" + os.urandom(32)
    return b"\x04" + (1_700_000_000).to_bytes(4) + bytes([22, len(oid)]) + oid + \
        (len(point) * 8 - 1).to_bytes(2) + point


def fingerprint(body: bytes) -> str:  # RFC 4880 §12.2
    return hashlib.sha1(b"\x99" + len(body).to_bytes(2) + body).hexdigest().upper()


def run(tmp_path, text: str, expected: str):
    key = tmp_path / "key.asc"
    key.write_text(text)
    return subprocess.run(["python3", "-I", str(SCRIPT), str(key), expected],
                          capture_output=True, text=True, check=False)


def test_accepts_matching_built_key(tmp_path):
    body = v4_key_body()
    text = armor(packet(6, body) + packet(13, b"Test <test@example.invalid>") + packet(2, b"\x04"))
    out = run(tmp_path, text, fingerprint(body))
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


@pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")
def test_accepts_gpg_generated_key_with_gpgs_fingerprint(tmp_path):
    """A real key: primary, user id, signatures and an encryption subkey (tags 6, 13, 2, 14)."""
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch"]
    subprocess.run(gpg + ["--pinentry-mode", "loopback", "--passphrase", "", "--quick-gen-key",
                          "Test <test@example.invalid>", "default", "default", "never"],
                   check=True, capture_output=True)
    listing = subprocess.run(gpg + ["--list-keys", "--with-colons"], check=True,
                             capture_output=True, text=True).stdout
    expected = next(ln.split(":")[9] for ln in listing.splitlines() if ln.startswith("fpr:"))
    text = subprocess.run(gpg + ["--armor", "--export"], check=True, capture_output=True,
                          text=True).stdout
    assert ckf.primary_fingerprints(text) == [expected]
    assert run(tmp_path, text, expected).returncode == 0
    other = expected[:-1] + ("1" if expected[-1] == "0" else "0")
    assert run(tmp_path, text, other).returncode == 1


def test_refuses_mismatch(tmp_path):
    body = v4_key_body()
    out = run(tmp_path, armor(packet(6, body)), fingerprint(v4_key_body()))
    assert out.returncode == 1 and "mismatch" in out.stderr


def test_refuses_two_primary_keys(tmp_path):
    body = v4_key_body()
    out = run(tmp_path, armor(packet(6, body) + packet(6, v4_key_body())), fingerprint(body))
    assert out.returncode == 1


@pytest.mark.parametrize("tag", [17, 12, 1, 60])
def test_refuses_unexpected_packet_tags(tag):
    with pytest.raises(ValueError, match="packet"):
        ckf.primary_fingerprints(armor(packet(6, v4_key_body()) + packet(tag, b"x")))


def test_refuses_new_format_partial_length():
    data = packet(6, v4_key_body()) + bytes([0xC0 | 13, 224]) + b"x"
    with pytest.raises(ValueError, match="partial"):
        ckf.primary_fingerprints(armor(data))


def test_refuses_old_format_indeterminate_length():
    data = packet(6, v4_key_body()) + bytes([0x80 | (13 << 2) | 3]) + b"Test"
    with pytest.raises(ValueError, match="indeterminate"):
        ckf.primary_fingerprints(armor(data))


def test_refuses_truncated_packet():
    data = packet(6, v4_key_body())[:-5]
    with pytest.raises(ValueError, match="truncated"):
        ckf.primary_fingerprints(armor(data))


def test_refusal_is_a_plain_exit_not_a_traceback(tmp_path):
    data = packet(6, v4_key_body()) + bytes([0xC0 | 13, 224]) + b"x"
    out = run(tmp_path, armor(data), "00")
    assert out.returncode == 1 and "Traceback" not in out.stderr
    assert "partial" in out.stderr
