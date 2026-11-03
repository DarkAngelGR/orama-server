from pathlib import Path, PurePosixPath

import pytest

from orama.platform import Platform, default_roots, detect_platform, has_nvidia, render_gid

FIXTURES = Path(__file__).parent / "fixtures" / "os-release"


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("ubuntu-24.04", Platform.UBUNTU),
        ("debian-12", Platform.DEBIAN),
        ("ugos-pro", Platform.UGOS),
        ("fedora-40", Platform.UNSUPPORTED),
    ],
)
def test_detect_platform(fixture, expected):
    assert detect_platform((FIXTURES / fixture).read_text()) == expected


def test_detect_platform_empty_is_unsupported():
    assert detect_platform("") == Platform.UNSUPPORTED


def test_default_roots():
    assert default_roots(Platform.UGOS) == (
        PurePosixPath("/volume1/data"),
        PurePosixPath("/volume1/docker/orama"),
    )
    assert default_roots(Platform.UBUNTU) == (PurePosixPath("/data"), PurePosixPath("/opt/orama"))


def test_render_gid_missing_device_is_none(tmp_path):
    assert render_gid(tmp_path / "renderD128") is None


def test_render_gid_reads_group(tmp_path):
    dev = tmp_path / "renderD128"
    dev.touch()
    assert render_gid(dev) == dev.stat().st_gid


@pytest.mark.parametrize(
    "present,expected",
    [("nvidia-smi", True), ("nvidia-ctk", True), ("nvidia-container-cli", True), ("ls", False)],
)
def test_has_nvidia(present, expected):
    assert has_nvidia(lambda name: f"/usr/bin/{name}" if name == present else None) is expected
