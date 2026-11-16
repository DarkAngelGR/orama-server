"""Jarvis recommendation engine contract tests."""

from orama.recommendations import apply_model_ranking, rank


def test_likes_outweigh_completion_and_continue_watching_is_neutral():
    watched = [
        {"kind": "movie", "source_id": 1, "genres": ["Sci-Fi"], "liked": True},
        {"kind": "movie", "source_id": 2, "genres": ["Drama"], "completed": True},
        {"kind": "series", "source_id": 3, "genres": ["Comedy"], "progress": 0.4},
    ]
    candidates = [
        {"kind": "movie", "source_id": 9, "title": "A", "genres": ["Drama"]},
        {"kind": "movie", "source_id": 10, "title": "B", "genres": ["Sci-Fi"]},
        {"kind": "series", "source_id": 11, "title": "C", "genres": ["Comedy"]},
    ]
    assert [r.candidate.source_id for r in rank(watched, candidates)] == [10, 9, 11]


def test_excludes_watched_and_existing_library_and_duplicates():
    watched = [{"kind": "movie", "source_id": 1}]
    candidates = [
        {"kind": "movie", "source_id": 1, "title": "Watched"},
        {"kind": "movie", "source_id": 2, "title": "Existing"},
        {"kind": "movie", "source_id": 3, "title": "New"},
        {"kind": "movie", "source_id": 3, "title": "Duplicate"},
    ]
    result = rank(watched, candidates, excluded={("movie", 2)})
    assert [r.candidate.source_id for r in result] == [3]


def test_bad_candidates_are_rejected():
    candidates = [
        {"kind": "movie", "source_id": True, "title": "Bad"},
        {"kind": "movie", "source_id": 4, "title": "<b>" * 100},
        {"kind": "movie", "source_id": 5, "title": "Bad genres", "genres": [123]},
        {"kind": "episode", "source_id": 6, "title": "Wrong type"},
        {"kind": "series", "source_id": 7, "title": "Valid"},
    ]
    assert [r.candidate.source_id for r in rank([], candidates)] == [7]


def test_model_cannot_invent_recommendations_or_insert_html():
    base = rank([], [
        {"kind": "movie", "source_id": 2, "title": "Two"},
        {"kind": "movie", "source_id": 1, "title": "One"},
    ])
    proposed = {"ids": [
        {"kind": "movie", "source_id": 2, "title": "<script>alert(1)</script>"},
        {"kind": "movie", "source_id": 999, "title": "Invented"},
        {"kind": "movie", "source_id": 2},
        {"kind": "movie", "source_id": True},
    ]}
    result = apply_model_ranking(base, proposed)
    assert [r.candidate.source_id for r in result] == [2, 1]
    assert result[0].candidate.title == "Two"


def test_invalid_model_output_returns_baseline():
    base = rank([], [{"kind": "movie", "source_id": 1, "title": "One"}])
    assert apply_model_ranking(base, {"ids": "oops"}) == base
