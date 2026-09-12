import pytest

from pipeline.scoring.transit_lines import compute_line_scores, ALL_LINES


@pytest.fixture(scope="module")
def scores():
    return compute_line_scores()


def test_every_line_has_a_score(scores):
    for line in ALL_LINES:
        key = f"{line.mode}:{line.line_id}"
        assert key in scores


def test_scores_within_0_100(scores):
    for key, score in scores.items():
        assert 0 <= score <= 100


def test_bens_named_best_lines_score_highly(scores):
    # Ben's examples: M14, M1, M4, RER A, M5, RER E should all be
    # comfortably in the upper half of their mode's scores.
    named_best = ["metro:14", "metro:1", "metro:4", "rer:A", "metro:5", "rer:E"]
    for key in named_best:
        mode = key.split(":")[0]
        mode_scores = [v for k, v in scores.items() if k.startswith(f"{mode}:")]
        median = sorted(mode_scores)[len(mode_scores) // 2]
        assert scores[key] >= median, f"{key} scored {scores[key]}, below its mode's median {median}"


def test_metro_line_1_and_4_are_top_two():
    scores = compute_line_scores()
    metro_scores = {k: v for k, v in scores.items() if k.startswith("metro:")}
    top_two = sorted(metro_scores, key=lambda k: -metro_scores[k])[:2]
    assert set(top_two) == {"metro:1", "metro:4"}


def test_rer_a_is_top_rer_line():
    scores = compute_line_scores()
    rer_scores = {k: v for k, v in scores.items() if k.startswith("rer:")}
    top = max(rer_scores, key=lambda k: rer_scores[k])
    assert top == "rer:A"


def test_quality_bonus_can_be_disabled():
    with_bonus = compute_line_scores(apply_quality_bonus=True)
    without_bonus = compute_line_scores(apply_quality_bonus=False)
    assert with_bonus["metro:4"] >= without_bonus["metro:4"]
