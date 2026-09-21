"""The scoring maths for the human-label audit (analysis/score_labels.py), on made-up labels."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import score_labels as S  # noqa: E402


def test_wilson_interval_matches_known_values():
    lo, hi = S.wilson(0, 20)
    assert lo == 0.0 and 0.15 < hi < 0.17            # zero misses in 20 still allows up to ~16%
    lo, hi = S.wilson(20, 20)
    assert 0.83 < lo < 0.85 and hi == 1.0
    lo, hi = S.wilson(9, 10)
    assert 0.59 < lo < 0.60 and 0.98 < hi < 0.99


def test_kappa_is_one_for_identical_raters_and_zero_for_chance():
    a = ["A", "B", "A", "B", "A", "B", "A", "B"]
    assert S.cohen_kappa(a, a) == 1.0
    b = ["A", "A", "B", "B", "A", "A", "B", "B"]         # agrees on exactly half, as chance predicts
    assert S.cohen_kappa(a, b) == pytest.approx(0.0)
    assert S.cohen_kappa(a, ["B" if x == "A" else "A" for x in a]) == pytest.approx(-1.0)


def make_scored(flag_true, flag_n, unflag_true, unflag_n):
    """A scored frame: `flag_true` of `flag_n` flagged items are real findings, and each unflagged stratum has
    `unflag_true` of `unflag_n`."""
    rows = []
    for i in range(flag_n):
        rows.append(dict(item_id=f"f{i}", stratum="FLAG_CONTAMINATION", repeat_of_item=None,
                         label="LAB_FINDING" if i < flag_true else "NONE", about="YES"))
    for s in S.UNFLAGGED:
        for i in range(unflag_n):
            rows.append(dict(item_id=f"{s}{i}", stratum=s, repeat_of_item=None,
                             label="LAB_FINDING" if i < unflag_true else "NONE", about="YES"))
    return pd.DataFrame(rows)


FRAME = {"pool_sizes": {"FLAG_CONTAMINATION": 100, "UNFLAGGED_HIGH": 200, "UNFLAGGED_STRONG": 2000, "UNFLAGGED_WEAK": 8000}}


def test_recall_is_estimated_from_pool_sizes():
    """Flagged pool 100 at 90% precision -> 90 true positives. At 1% in the two extrapolated pools (200 and 2000)
    -> 22 missed. The weak pool (8000) is NOT extrapolated. Recall = 90 / (90 + 22)."""
    est = S.estimate_recall(make_scored(18, 20, 1, 100), FRAME)
    assert est["true_findings_flagged"] == pytest.approx(90)
    assert est["true_findings_missed"] == pytest.approx(22)
    assert est["recall"] == pytest.approx(90 / 112)
    assert est["recall_ci"][0] < 0.75 < est["recall_ci"][1]


def test_no_misses_found_means_recall_near_one_with_a_still_honest_interval():
    est = S.estimate_recall(make_scored(20, 20, 0, 40), FRAME)
    assert est["recall"] == 1.0 and est["true_findings_missed"] == 0


def test_a_finding_about_another_incident_is_not_a_positive():
    row = {"label": "LAB_FINDING", "about": "NO_OTHER_INCIDENT"}
    assert not S.is_positive(row, "LAB_FINDING")
    assert S.is_positive({"label": "LAB_FINDING", "about": "UNSURE"}, "LAB_FINDING", strict=False)
    assert not S.is_positive({"label": "LAB_FINDING", "about": "UNSURE"}, "LAB_FINDING", strict=True)


def test_validation_names_missing_and_invalid_answers():
    df = pd.DataFrame([dict(item_id="L001", label="LAB_FINDING", about="YES", notes=""),
                       dict(item_id="L002", label="", about="YES", notes=""),
                       dict(item_id="L003", label="MAYBE", about="", notes="")])
    problems = " | ".join(S.validate(df, "coder"))
    assert "L002" in problems and "L003" in problems and "MAYBE" in problems


def test_stratum_table_reports_precision_and_miss_rate_with_intervals():
    t = S.stratum_table(make_scored(18, 20, 1, 100), "coder").set_index("stratum")
    assert t.loc["FLAG_CONTAMINATION", "rate"] == pytest.approx(0.9)
    assert t.loc["UNFLAGGED_HIGH", "rate"] == pytest.approx(0.01)
    assert t.loc["UNFLAGGED_HIGH", "ci_high"] > 0.01 > t.loc["UNFLAGGED_HIGH", "ci_low"]


def test_the_weak_stratum_never_moves_the_recall_estimate():
    """One stray positive among a few weak-cue sentences (pool 8000) must not change recall."""
    base = S.estimate_recall(make_scored(18, 20, 1, 100), FRAME)["recall"]
    noisy = make_scored(18, 20, 1, 100)
    idx = noisy[noisy.stratum == "UNFLAGGED_WEAK"].index[-1]
    noisy.loc[idx, "label"] = "LAB_FINDING"
    assert S.estimate_recall(noisy, FRAME)["recall"] == pytest.approx(base)


def test_an_interval_always_contains_its_own_point_estimate():
    for k, n, pool in ((9, 9, 11), (0, 14, 2579), (5, 14, 178)):
        lo, hi = S.wilson_or_census(k, n, pool)
        assert lo <= k / n <= hi
    assert S.wilson_or_census(5, 11, 11) == (5 / 11, 5 / 11)      # a census has no sampling error


def test_a_wide_recall_interval_is_called_too_wide_to_measure():
    wide = {"recall": 0.3, "recall_ci": (0.1, 0.9), "recall_worst": 0.05, "precision": 0.9, "true_findings_flagged": 100,
            "true_findings_missed": 200, "missed_worst": 1900, "missed_ci": (10, 500)}
    narrow = dict(wide, recall_ci=(0.7, 0.85), recall=0.78, recall_worst=0.7)
    assert "too wide" in S.describe_recall(wide) and "worst case" in S.describe_recall(wide)
    assert "too wide" not in S.describe_recall(narrow)


def test_no_misses_found_still_has_a_worst_case_below_one():
    """0 misses in 14 sentences from a pool of 2579 must not read as certain: the bootstrap has no spread, the
    worst case does."""
    est = S.estimate_recall(make_scored(18, 20, 0, 14), FRAME)
    assert est["recall"] == 1.0 and est["recall_ci"] == (1.0, 1.0)
    assert est["recall_worst"] < 0.5
    assert "too wide" in S.describe_recall(est)


def test_a_miss_means_the_same_in_the_table_and_the_estimate():
    """Unflagged sentences called LAB_CLEAN are not misses of the finding rule in either place."""
    sc = make_scored(18, 20, 0, 100)
    idx = sc[sc.stratum == "UNFLAGGED_HIGH"].index[:5]
    sc.loc[idx, "label"] = "LAB_CLEAN"
    t = S.stratum_table(sc, "coder", FRAME["pool_sizes"]).set_index("stratum")
    assert t.loc["UNFLAGGED_HIGH", "k"] == 0
    assert S.estimate_recall(sc, FRAME)["true_findings_missed"] == 0
