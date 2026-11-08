"""Post-import subtitle readiness assessment."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

SubtitleStatus = Literal["ready", "partial", "missing", "unknown"]

_ISO3_TO_ISO2 = {
    "ell": "el",
    "gre": "el",
    "eng": "en",
    "fra": "fr",
    "fre": "fr",
    "deu": "de",
    "ger": "de",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "nld": "nl",
    "dut": "nl",
    "rus": "ru",
    "jpn": "ja",
    "kor": "ko",
    "zho": "zh",
    "chi": "zh",
    "ara": "ar",
    "tur": "tr",
    "pol": "pl",
}

_IMAGE_SUB_FORMATS = frozenset({"pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle"})


@dataclass(frozen=True)
class SubtitleAssessment:
    """Immutable subtitle readiness assessment for an imported media item."""

    status: SubtitleStatus
    available_languages: tuple[str, ...]
    missing_languages: tuple[str, ...]
    reason_codes: tuple[str, ...]
    note: str
    needs_translation: bool = False


def _norm_lang(code: Any) -> str | None:
    if not isinstance(code, str):
        return None
    cleaned = code.strip().lower()
    if not cleaned:
        return None
    if cleaned in _ISO3_TO_ISO2:
        return _ISO3_TO_ISO2[cleaned]
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned
    return None


def _extract_languages_and_formats(
    available: Iterable[Any],
) -> tuple[list[str], set[str], set[str]]:
    """Extract normalized language codes, text formats, and image formats."""
    langs: list[str] = []
    text_formats: set[str] = set()
    image_formats: set[str] = set()

    for entry in available:
        if isinstance(entry, str):
            norm = _norm_lang(entry)
            if norm and norm not in langs:
                langs.append(norm)
        elif isinstance(entry, Mapping):
            raw_code = entry.get("code2") or entry.get("language") or entry.get("code")
            norm = _norm_lang(raw_code)
            if norm and norm not in langs:
                langs.append(norm)
            fmt = str(entry.get("format") or entry.get("codec") or "").strip().lower()
            if fmt:
                if fmt in _IMAGE_SUB_FORMATS:
                    image_formats.add(fmt)
                else:
                    text_formats.add(fmt)

    return langs, text_formats, image_formats


def assess_subtitles(
    available_languages: Iterable[Any] | None,
    wanted_languages: Sequence[str],
) -> SubtitleAssessment:
    """Assess available subtitles against the user's ordered ``wanted_languages``."""
    wanted: list[str] = []
    for raw in wanted_languages:
        norm = _norm_lang(raw)
        if norm and norm not in wanted:
            wanted.append(norm)

    if not wanted:
        wanted = ["el", "en"]

    if available_languages is None:
        return SubtitleAssessment(
            status="unknown",
            available_languages=(),
            missing_languages=tuple(wanted),
            reason_codes=("subtitles_not_checked",),
            note="Subtitles not checked yet",
            needs_translation=False,
        )

    avail_list, text_formats, image_formats = _extract_languages_and_formats(
        available_languages
    )
    avail_set = set(avail_list)
    missing = [lang for lang in wanted if lang not in avail_set]
    primary = wanted[0]
    primary_upper = primary.upper()

    codes: list[str] = []
    image_only = bool(image_formats and not text_formats)
    if image_only:
        codes.append("image_subtitle_format")

    if primary in avail_set:
        note = f"Ready · {primary_upper} subs"
        if image_only:
            note += " (image subtitles may require burn-in)"
        return SubtitleAssessment(
            status="ready",
            available_languages=tuple(avail_list),
            missing_languages=tuple(missing),
            reason_codes=tuple(codes),
            note=note,
            needs_translation=False,
        )

    present_fallbacks = [lang.upper() for lang in wanted[1:] if lang in avail_set]
    if present_fallbacks:
        codes.append("missing_primary_subtitle")
        fb_str = ", ".join(present_fallbacks)
        note = f"Ready · {fb_str} subs, still looking for {primary_upper}"
        if image_only:
            note += " (image subtitles may require burn-in)"
        return SubtitleAssessment(
            status="partial",
            available_languages=tuple(avail_list),
            missing_languages=tuple(missing),
            reason_codes=tuple(codes),
            note=note,
            needs_translation=True,
        )

    codes.append("missing_all_subtitles")
    wanted_upper = ", ".join(lang.upper() for lang in wanted)
    return SubtitleAssessment(
        status="missing",
        available_languages=tuple(avail_list),
        missing_languages=tuple(missing),
        reason_codes=tuple(codes),
        note=f"Ready · still looking for {wanted_upper} subs",
        needs_translation=True,
    )


def assess_episode_subtitles(
    episodes: Sequence[Mapping[str, Any]],
    wanted_languages: Sequence[str],
) -> SubtitleAssessment:
    """Assess subtitle readiness across multiple episodes or a partial season."""
    wanted: list[str] = []
    for raw in wanted_languages:
        norm = _norm_lang(raw)
        if norm and norm not in wanted:
            wanted.append(norm)
    if not wanted:
        wanted = ["el", "en"]

    if not episodes:
        return assess_subtitles(None, wanted)

    assessments = [
        assess_subtitles(ep.get("available"), wanted) for ep in episodes
    ]
    total = len(assessments)
    primary = wanted[0]
    primary_upper = primary.upper()
    ready_count = sum(1 for a in assessments if a.status == "ready")

    all_avail: list[str] = []
    all_missing: list[str] = []
    all_codes: list[str] = []
    for a in assessments:
        for lang in a.available_languages:
            if lang not in all_avail:
                all_avail.append(lang)
        for lang in a.missing_languages:
            if lang not in all_missing:
                all_missing.append(lang)
        for code in a.reason_codes:
            if code not in all_codes:
                all_codes.append(code)

    if ready_count == total:
        return SubtitleAssessment(
            status="ready",
            available_languages=tuple(all_avail),
            missing_languages=tuple(all_missing),
            reason_codes=tuple(all_codes),
            note=f"Ready · {primary_upper} subs",
            needs_translation=False,
        )

    if ready_count > 0 or any(a.status == "partial" for a in assessments):
        if "missing_primary_subtitle" not in all_codes:
            all_codes.append("missing_primary_subtitle")
        return SubtitleAssessment(
            status="partial",
            available_languages=tuple(all_avail),
            missing_languages=tuple(all_missing),
            reason_codes=tuple(all_codes),
            note=(
                f"Ready · {ready_count}/{total} episodes have {primary_upper} subs, "
                f"still looking for {primary_upper}"
            ),
            needs_translation=True,
        )

    if "missing_all_subtitles" not in all_codes:
        all_codes.append("missing_all_subtitles")
    wanted_upper = ", ".join(lang.upper() for lang in wanted)
    return SubtitleAssessment(
        status="missing",
        available_languages=tuple(all_avail),
        missing_languages=tuple(all_missing),
        reason_codes=tuple(all_codes),
        note=f"Ready · still looking for {wanted_upper} subs",
        needs_translation=True,
    )
