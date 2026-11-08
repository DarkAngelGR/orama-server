"""Tests for post-import playback compatibility assessment."""
from __future__ import annotations

import pytest

from orama.requests.playback import PlaybackAssessment, assess_playback


@pytest.mark.parametrize(
    "audio_codec,expected_code",
    [
        ("DTS", "audio_dts"),
        ("DTS-HD MA", "audio_dts_hd_ma"),
        ("DTS-X", "audio_dts_x"),
        ("DTS-HD HRA", "audio_dts_hd_hra"),
        ("DTS-ES", "audio_dts_es"),
    ],
)
def test_s95f_dts_variants_flagged_as_may_transcode(audio_codec, expected_code):
    media_info = {
        "videoCodec": "x265",
        "audioCodec": audio_codec,
        "audioChannels": 5.1,
        "resolution": "3840x2160",
        "videoBitDepth": 10,
        "videoDynamicRange": "HDR",
        "videoDynamicRangeType": "HDR10",
    }
    res = assess_playback(media_info, "samsung_s95f")
    assert isinstance(res, PlaybackAssessment)
    assert res.status == "may_transcode"
    assert res.verified_device is True
    assert expected_code in res.reason_codes
    assert res.needs_conversion is True
    assert res.note.startswith("Ready · may transcode (")
    assert "DTS" in res.note


def test_s95f_multichannel_lpcm_and_flac():
    lpcm_51 = {
        "videoCodec": "h264",
        "audioCodec": "PCM",
        "audioChannels": 5.1,
        "resolution": "1920x1080",
    }
    res_lpcm = assess_playback(lpcm_51, "samsung_s95f")
    assert res_lpcm.status == "may_transcode"
    assert "audio_pcm_multichannel" in res_lpcm.reason_codes
    assert "LPCM" in res_lpcm.note

    # Stereo PCM is fine
    lpcm_20 = {
        "videoCodec": "h264",
        "audioCodec": "PCM",
        "audioChannels": 2.0,
        "resolution": "1920x1080",
    }
    res_stereo = assess_playback(lpcm_20, "samsung_s95f")
    assert res_stereo.status == "direct_play"

    flac_info = {
        "videoCodec": "h264",
        "audioCodec": "FLAC",
        "audioChannels": 2.0,
        "resolution": "1920x1080",
    }
    res_flac = assess_playback(flac_info, "samsung_s95f")
    assert res_flac.status == "may_transcode"
    assert "audio_flac" in res_flac.reason_codes
    assert "FLAC" in res_flac.note


def test_s95f_dolby_vision_variants():
    # DV Profile 5 (no HDR10 fallback)
    dv5 = {
        "videoCodec": "hevc",
        "audioCodec": "EAC3",
        "videoDynamicRange": "Dolby Vision",
        "videoDynamicRangeType": "DV Profile 5",
    }
    res_dv5 = assess_playback(dv5, "samsung_s95f")
    assert res_dv5.status == "may_transcode"
    assert "video_dv_no_fallback" in res_dv5.reason_codes
    assert "Dolby Vision" in res_dv5.note

    # DV without HDR fallback
    dv_no_fb = {
        "videoCodec": "hevc",
        "audioCodec": "EAC3",
        "videoDynamicRange": "Dolby Vision",
        "videoDynamicRangeType": "DV (w/o HDR fallback)",
    }
    res_no_fb = assess_playback(dv_no_fb, "samsung_s95f")
    assert res_no_fb.status == "may_transcode"
    assert "video_dv_no_fallback" in res_no_fb.reason_codes

    # DV with HDR10 fallback on S95F vs LG
    dv_hdr10 = {
        "videoCodec": "hevc",
        "audioCodec": "EAC3",
        "videoDynamicRange": "Dolby Vision",
        "videoDynamicRangeType": "Dolby Vision (HDR10)",
    }
    res_s95f_dv_fb = assess_playback(dv_hdr10, "samsung_s95f")
    assert res_s95f_dv_fb.status == "may_transcode"
    assert "video_dv" in res_s95f_dv_fb.reason_codes
    assert "video_dv_no_fallback" not in res_s95f_dv_fb.reason_codes

    res_lg_dv_fb = assess_playback(dv_hdr10, "lg")
    assert res_lg_dv_fb.status == "direct_play"
    assert res_lg_dv_fb.verified_device is False
    assert "best-effort" in res_lg_dv_fb.note


def test_s95f_truehd_and_hdr10_direct_play_vs_safest():
    truehd_hdr10plus = {
        "videoCodec": "hevc",
        "audioCodec": "TrueHD Atmos",
        "resolution": "3840x2160",
        "videoBitDepth": 10,
        "videoDynamicRange": "HDR",
        "videoDynamicRangeType": "HDR10+",
    }
    res_s95f = assess_playback(truehd_hdr10plus, "samsung_s95f")
    assert res_s95f.status == "direct_play"
    assert res_s95f.needs_conversion is False
    assert res_s95f.verified_device is True

    # On "safest" or "apple_tv", TrueHD is unplayable and flagged as may transcode
    res_safest = assess_playback(truehd_hdr10plus, "safest")
    assert res_safest.status == "may_transcode"
    assert "audio_truehd_atmos" in res_safest.reason_codes

    res_atv = assess_playback(truehd_hdr10plus, "apple_tv")
    assert res_atv.status == "may_transcode"
    assert "audio_truehd_atmos" in res_atv.reason_codes


def test_unknown_audio_video_or_dv_profile_and_missing_media_info():
    # Missing media_info is never verified Direct Play
    assert assess_playback(None, "samsung_s95f").status == "unknown"
    assert assess_playback({}, "samsung_s95f").status == "unknown"
    assert "playback not checked" in assess_playback(None, "samsung_s95f").note

    # Unknown audio codec
    unk_audio = {"videoCodec": "h264", "audioCodec": "WMAPRO"}
    res_audio = assess_playback(unk_audio, "samsung_s95f")
    assert res_audio.status == "unknown"
    assert "unknown_audio_codec" in res_audio.reason_codes
    assert "playback not checked" in res_audio.note

    # Unknown video codec
    unk_video = {"videoCodec": "RV40", "audioCodec": "AAC"}
    res_video = assess_playback(unk_video, "samsung_s95f")
    assert res_video.status == "unknown"
    assert "unknown_video_codec" in res_video.reason_codes

    # Unknown DV profile
    unk_dv = {
        "videoCodec": "hevc",
        "audioCodec": "AAC",
        "videoDynamicRange": "Dolby Vision",
        "videoDynamicRangeType": "Dolby Vision Profile Unknown",
    }
    res_dv = assess_playback(unk_dv, "lg")
    assert res_dv.status == "unknown"
    assert "unknown_dv_profile" in res_dv.reason_codes


def test_text_versus_image_subtitles_in_media_info():
    text_subs = {
        "videoCodec": "h264",
        "audioCodec": "AAC",
        "subtitleCodecs": "SRT",
    }
    assert assess_playback(text_subs, "samsung_s95f").status == "direct_play"

    image_subs = {
        "videoCodec": "h264",
        "audioCodec": "AAC",
        "subtitleCodecs": "PGS",
    }
    res_pgs = assess_playback(image_subs, "samsung_s95f")
    assert res_pgs.status == "may_transcode"
    assert "subtitle_image" in res_pgs.reason_codes
    assert "PGS" in res_pgs.note or "image subtitles" in res_pgs.note


def test_typed_followup_reasons_refuse_paths_or_commands(tmp_path):
    from orama.requests.models import RequestSpec
    from orama.requests.store import RequestStore

    store = RequestStore(tmp_path / "requests.sqlite3")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=101), 1000.0)

    res = assess_playback(
        {"videoCodec": "hevc", "audioCodec": "DTS"}, "samsung_s95f"
    )
    assert res.needs_conversion is True
    for code in res.reason_codes:
        store.record_followup(rec.id, "conversion", code, 1005.0)

    followups = store.list_followups("conversion")
    assert len(followups) == 1
    assert followups[0]["request_id"] == rec.id
    assert followups[0]["reason_code"] == "audio_dts"

    # Untrusted media paths or shell strings are refused
    with pytest.raises(ValueError):
        store.record_followup(rec.id, "conversion", "/data/media/movies/bad.mkv", 1005.0)
    with pytest.raises(ValueError):
        store.record_followup(rec.id, "exec", "audio_dts", 1005.0)
