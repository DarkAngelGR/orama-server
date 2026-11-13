"""Post-import playback compatibility assessment from Sonarr/Radarr mediaInfo."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

import yaml

PlaybackStatus = Literal["direct_play", "may_transcode", "unknown"]

_KNOWN_VIDEO_CODECS = {
    "h264": "h264",
    "x264": "h264",
    "avc": "h264",
    "avc1": "h264",
    "hevc": "hevc",
    "h265": "hevc",
    "x265": "hevc",
    "hev1": "hevc",
    "av1": "av1",
    "mpeg4": "mpeg4",
    "vp9": "vp9",
}

_IMAGE_SUBTITLE_TOKENS = frozenset({"pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle"})


@dataclass(frozen=True)
class PlaybackAssessment:
    """Immutable playback compatibility result for an imported file."""

    status: PlaybackStatus
    verified_device: bool
    reasons: tuple[str, ...]
    reason_codes: tuple[str, ...]
    note: str
    needs_conversion: bool = False


def _load_device(device: str) -> dict[str, Any]:
    node = resources.files("orama.profiles").joinpath("devices", f"{device}.yaml")
    data = yaml.safe_load(node.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Invalid device profile: {device}")
    return data


def _classify_audio(raw_audio: str, channels: Any) -> tuple[str | None, str | None, bool]:
    """Return (symbol, human_label, is_known) for an audio codec string."""
    norm = " ".join(raw_audio.strip().lower().replace("_", " ").split())
    if not norm:
        return None, None, False

    if norm in ("dts-x", "dtsx", "dts x", "dts:x"):
        return "dts_x", "DTS:X audio", True
    if norm in ("dts-hd ma", "dts hd ma", "dtshdma"):
        return "dts_hd_ma", "DTS-HD MA audio", True
    if norm in ("dts-hd hra", "dts hd hra", "dtshdhra"):
        return "dts_hd_hra", "DTS-HD HRA audio", True
    if norm in ("dts-es", "dts es", "dtses"):
        return "dts_es", "DTS-ES audio", True
    if norm == "dts" or norm.startswith("dts "):
        return "dts", "DTS audio", True

    if "truehd" in norm and "atmos" in norm:
        return "truehd_atmos", "TrueHD Atmos audio", True
    if norm == "truehd":
        return "truehd", "TrueHD audio", True

    if norm in ("pcm", "lpcm", "pcm s16le", "pcm s24le"):
        try:
            ch = float(channels) if channels is not None else 6.0
        except (TypeError, ValueError):
            ch = 6.0
        if ch <= 2.0:
            return "pcm_stereo", "stereo PCM audio", True
        return "pcm_multichannel", "multichannel LPCM audio", True

    if norm == "flac":
        return "flac", "FLAC audio", True

    if norm in (
        "aac",
        "ac3",
        "ac-3",
        "eac3",
        "e-ac3",
        "e-ac-3",
        "dd",
        "dd+",
        "ddp",
        "eac3 atmos",
        "mp3",
        "opus",
        "vorbis",
    ):
        return None, None, True

    return None, None, False


def _classify_dynamic_range(
    dyn_range: str, dyn_type: str
) -> tuple[str | None, str | None, bool]:
    """Return (symbol, human_label, is_unknown_dv) for video dynamic range metadata."""
    combined = f"{dyn_range} {dyn_type}".strip().lower()
    if not combined:
        return None, None, False

    if "dolby vision" in combined or "dv" in combined.split() or combined.startswith("dv"):
        if "unknown" in combined or "?" in combined:
            return None, "unrecognized Dolby Vision profile", True
        has_hdr_fallback = any(
            tok in combined
            for tok in ("hdr10", "hdr fallback", "profile 7", "profile 8", "p7", "p8")
        ) and not any(tok in combined for tok in ("w/o hdr", "without hdr", "no hdr"))
        if not has_hdr_fallback or any(
            tok in combined for tok in ("profile 5", "dv5", "p5", "w/o hdr", "without hdr")
        ):
            return "dv_no_fallback", "Dolby Vision without HDR fallback", False
        return "dv", "Dolby Vision", False

    if "hdr10+" in combined or "hdr10plus" in combined:
        return "hdr10_plus", "HDR10+", False
    if "hdr10" in combined or "hdr" in combined or "pq" in combined:
        return "hdr10", "HDR10", False

    return None, None, False


def assess_playback(
    media_info: Mapping[str, Any] | None, device: str = "safest"
) -> PlaybackAssessment:
    """Assess whether an imported file's mediaInfo can direct-play on ``device``."""
    try:
        dev = _load_device(device)
    except (OSError, TypeError, ValueError):
        dev = _load_device("safest")

    verified = bool(dev.get("verified", False))
    dev_name = str(dev.get("name", device))
    unplayable = set(dev.get("unplayable") or ())
    caps = dev.get("capabilities") or {}
    supported_video = set(caps.get("video_codecs") or _KNOWN_VIDEO_CODECS.values())
    image_subs = set(caps.get("image_subtitles_burn_in") or _IMAGE_SUBTITLE_TOKENS)

    if not isinstance(media_info, Mapping):
        return PlaybackAssessment(
            status="unknown",
            verified_device=verified,
            reasons=("playback not checked",),
            reason_codes=("missing_media_info",),
            note="Ready · playback not checked",
            needs_conversion=False,
        )

    raw_video = str(media_info.get("videoCodec") or "").strip()
    raw_audio = str(media_info.get("audioCodec") or "").strip()
    if not raw_video or not raw_audio:
        return PlaybackAssessment(
            status="unknown",
            verified_device=verified,
            reasons=("playback not checked",),
            reason_codes=("missing_media_info",),
            note="Ready · playback not checked",
            needs_conversion=False,
        )

    video_key = _KNOWN_VIDEO_CODECS.get(raw_video.lower())
    if video_key is None:
        return PlaybackAssessment(
            status="unknown",
            verified_device=verified,
            reasons=("unrecognized video format",),
            reason_codes=("unknown_video_codec",),
            note="Ready · playback not checked (unrecognized video format)",
            needs_conversion=False,
        )

    audio_sym, audio_label, audio_known = _classify_audio(
        raw_audio, media_info.get("audioChannels")
    )
    if not audio_known:
        return PlaybackAssessment(
            status="unknown",
            verified_device=verified,
            reasons=("unrecognized audio format",),
            reason_codes=("unknown_audio_codec",),
            note="Ready · playback not checked (unrecognized audio format)",
            needs_conversion=False,
        )

    dyn_sym, dyn_label, dv_unknown = _classify_dynamic_range(
        str(media_info.get("videoDynamicRange") or ""),
        str(media_info.get("videoDynamicRangeType") or ""),
    )
    if dv_unknown:
        return PlaybackAssessment(
            status="unknown",
            verified_device=verified,
            reasons=("unrecognized Dolby Vision profile",),
            reason_codes=("unknown_dv_profile",),
            note="Ready · playback not checked (unrecognized Dolby Vision profile)",
            needs_conversion=False,
        )

    reasons: list[str] = []
    codes: list[str] = []

    if video_key not in supported_video:  # a known codec this device cannot decode
        codes.append(f"video_codec_{video_key}")
        reasons.append(f"{raw_video} video")

    if dyn_sym is not None and dyn_sym in unplayable and dyn_label is not None:
        codes.append(f"video_{dyn_sym}")
        reasons.append(dyn_label)

    if audio_sym is not None and audio_sym in unplayable and audio_label is not None:
        codes.append(f"audio_{audio_sym}")
        reasons.append(audio_label)

    raw_sub_codecs = str(
        media_info.get("subtitleCodecs") or media_info.get("subtitleCodec") or ""
    ).strip()
    if raw_sub_codecs:
        tokens = {
            t.strip().lower()
            for t in raw_sub_codecs.replace("/", ",").split(",")
            if t.strip()
        }
        if tokens and tokens <= image_subs:
            codes.append("subtitle_image")
            reasons.append("image subtitles (PGS/VobSub)")

    if reasons:
        joined = ", ".join(reasons)
        return PlaybackAssessment(
            status="may_transcode",
            verified_device=verified,
            reasons=tuple(reasons),
            reason_codes=tuple(codes),
            note=f"Ready · may transcode ({joined})",
            needs_conversion=True,
        )

    if verified:
        note = f"Ready · direct play on {dev_name}"
    else:
        note = f"Ready · likely direct play on {dev_name} (best-effort profile)"

    return PlaybackAssessment(
        status="direct_play",
        verified_device=verified,
        reasons=(),
        reason_codes=(),
        note=note,
        needs_conversion=False,
    )
