"""Host platform detection."""

import shlex
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path, PurePosixPath


class Platform(StrEnum):
    UBUNTU = "ubuntu"
    DEBIAN = "debian"
    UGOS = "ugos"
    UNSUPPORTED = "unsupported"


def _parse_os_release(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = [raw.strip("\"'")]
        fields[key.strip()] = parts[0] if parts else ""
    return fields


def detect_platform(os_release: str) -> Platform:
    f = _parse_os_release(os_release)
    # UGOS reports ID=debian, so check its markers first. Markers are assumed:
    # verify against the real /etc/os-release on the device.
    if any("ugos" in f.get(k, "").lower() for k in ("NAME", "PRETTY_NAME", "ID_LIKE", "VARIANT")):
        return Platform.UGOS
    return {"ubuntu": Platform.UBUNTU, "debian": Platform.DEBIAN}.get(
        f.get("ID", "").lower(), Platform.UNSUPPORTED
    )


def default_roots(p: Platform) -> tuple[PurePosixPath, PurePosixPath]:
    """(data_root, config_root) defaults."""
    if p is Platform.UGOS:
        return PurePosixPath("/volume1/data"), PurePosixPath("/volume1/docker/orama")
    return PurePosixPath("/data"), PurePosixPath("/opt/orama")


def render_gid(dev: Path = Path("/dev/dri/renderD128")) -> int | None:
    """Numeric group id owning the GPU render node, or None if absent."""
    try:
        return dev.stat().st_gid
    except OSError:
        return None


NVIDIA_CDI_SPECS = (Path("/etc/cdi/nvidia.yaml"), Path("/var/run/cdi/nvidia.yaml"))


def has_nvidia(docker_runtimes: str, cdi_specs: Iterable[Path] = NVIDIA_CDI_SPECS) -> bool:
    """True if Docker can give containers an NVIDIA GPU.

    ``docker_runtimes`` is the output of ``docker info --format '{{json .Runtimes}}'``.
    NVIDIA drivers alone (``nvidia-smi``) are not enough: Docker needs its nvidia runtime
    or an NVIDIA CDI spec.
    """
    return "nvidia" in docker_runtimes or any(p.is_file() for p in cdi_specs)
