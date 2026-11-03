"""Fail unless an armored OpenPGP key file holds exactly one primary key with the given
v4 fingerprint. Used by the agent image build so Docker's apt key is never trusted on
first use; the slim base image has no gpg to do this.

Usage: python check_key_fingerprint.py KEY_FILE FINGERPRINT
"""

import base64
import hashlib
import sys

PUBLIC_KEY_TAG = 6
# Signature, public key, user id, public subkey: all an exported apt key holds.
ALLOWED_TAGS = frozenset({2, PUBLIC_KEY_TAG, 13, 14})


def _dearmor(text: str) -> bytes:
    lines = text.strip().splitlines()
    if not lines or lines[0] != "-----BEGIN PGP PUBLIC KEY BLOCK-----":
        raise ValueError("not an armored public key block")
    body = lines[lines.index("") + 1:]
    data = [ln for ln in body if not ln.startswith(("=", "-----END"))]
    return base64.b64decode("".join(data), validate=True)


def _packets(data: bytes):
    i = 0
    while i < len(data):
        head = data[i]
        if not head & 0x80:
            raise ValueError("bad packet header")
        if head & 0x40:  # new format
            tag, i = head & 0x3F, i + 1
            first = data[i]
            if first < 192:
                length, i = first, i + 1
            elif first < 224:
                length, i = ((first - 192) << 8) + data[i + 1] + 192, i + 2
            elif first == 255:
                length, i = int.from_bytes(data[i + 1:i + 5]), i + 5
            else:
                raise ValueError("partial-length packets are not expected in a key")
        else:  # old format
            tag, ltype, i = (head >> 2) & 0x0F, head & 0x03, i + 1
            if ltype == 3:
                raise ValueError("indeterminate-length packets are not expected in a key")
            size = 1 << ltype
            length, i = int.from_bytes(data[i:i + size]), i + size
        if tag not in ALLOWED_TAGS:
            raise ValueError(f"unexpected packet tag {tag} in a key")
        if i + length > len(data):
            raise ValueError("truncated packet")
        yield tag, data[i:i + length]
        i += length


def primary_fingerprints(text: str) -> list[str]:
    prints = []
    for tag, body in _packets(_dearmor(text)):
        if tag == PUBLIC_KEY_TAG:
            if body[0] != 4:
                raise ValueError(f"unsupported key version {body[0]}")
            digest = hashlib.sha1(b"\x99" + len(body).to_bytes(2) + body).hexdigest()
            prints.append(digest.upper())
    return prints


def main() -> int:
    path, expected = sys.argv[1], sys.argv[2].replace(" ", "").upper()
    try:
        with open(path, encoding="ascii") as f:
            found = primary_fingerprints(f.read())
    except (OSError, ValueError, IndexError) as e:
        print(f"key refused: {e}", file=sys.stderr)
        return 1
    if found != [expected]:
        print(f"key fingerprint mismatch: expected {expected}, found {found}", file=sys.stderr)
        return 1
    print(f"key fingerprint ok: {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
