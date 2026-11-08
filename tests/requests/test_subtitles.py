"""Tests for post-import subtitle readiness assessment."""
from __future__ import annotations

from orama.requests.subtitles import (
    SubtitleAssessment,
    assess_episode_subtitles,
    assess_subtitles,
)


def test_greek_present_marks_ready():
    res = assess_subtitles(["el", "en"], ("el", "en"))
    assert isinstance(res, SubtitleAssessment)
    assert res.status == "ready"
    assert res.needs_translation is False
    assert res.note == "Ready · EL subs"


def test_english_only_when_greek_is_first_reports_still_looking():
    res = assess_subtitles(["en"], ("el", "en"))
    assert res.status == "partial"
    assert res.needs_translation is True
    assert res.note == "Ready · EN subs, still looking for EL"
    assert "missing_primary_subtitle" in res.reason_codes


def test_no_subtitles_present():
    res = assess_subtitles([], ("el", "en"))
    assert res.status == "missing"
    assert res.needs_translation is True
    assert "still looking for EL" in res.note
    assert "missing_all_subtitles" in res.reason_codes


def test_changed_language_order():
    # When user puts English first and Greek second, English-only satisfies primary language
    res_en_first = assess_subtitles(["en"], ("en", "el"))
    assert res_en_first.status == "ready"
    assert res_en_first.needs_translation is False
    assert res_en_first.note == "Ready · EN subs"

    # And Greek-only when English is first reports still looking for EN
    res_el_only = assess_subtitles(["el"], ("en", "el"))
    assert res_el_only.status == "partial"
    assert res_el_only.needs_translation is True
    assert res_el_only.note == "Ready · EL subs, still looking for EN"


def test_text_versus_image_subtitle_objects():
    # Bazarr-style subtitle dicts with format/type info
    srt_subs = [{"code2": "el", "format": "srt"}]
    res_srt = assess_subtitles(srt_subs, ("el", "en"))
    assert res_srt.status == "ready"
    assert "image_subtitle_format" not in res_srt.reason_codes

    pgs_subs = [{"code2": "el", "format": "pgs"}]
    res_pgs = assess_subtitles(pgs_subs, ("el", "en"))
    assert "image_subtitle_format" in res_pgs.reason_codes
    assert "image" in res_pgs.note.lower() or "burn-in" in res_pgs.note.lower()


def test_multi_episode_and_partial_season_readiness():
    # Episode 1 has EL+EN, Episode 2 has only EN, Episode 3 has none
    episodes = [
        {"episode_id": 101, "available": ["el", "en"]},
        {"episode_id": 102, "available": ["en"]},
        {"episode_id": 103, "available": []},
    ]
    summary = assess_episode_subtitles(episodes, ("el", "en"))
    assert summary.status == "partial"
    assert summary.needs_translation is True
    assert "1/3" in summary.note
    assert "still looking for EL" in summary.note

    # All episodes have primary language -> ready
    all_ready = [
        {"episode_id": 101, "available": ["el"]},
        {"episode_id": 102, "available": ["el", "en"]},
    ]
    summary_ok = assess_episode_subtitles(all_ready, ("el", "en"))
    assert summary_ok.status == "ready"
    assert summary_ok.needs_translation is False
    assert summary_ok.note == "Ready · EL subs"


def test_missing_subtitle_observation_is_unknown_not_ready():
    res = assess_subtitles(None, ("el", "en"))
    assert res.status == "unknown"
    assert res.needs_translation is False
    assert "not checked" in res.note.lower()
