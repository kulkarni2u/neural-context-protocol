import math

import pytest

from ncp.eval import PairedResult, paired_comparison

A = [20.61, 17.58, 20.00, 18.18, 18.79, 20.00, 20.00, 20.00, 18.18, 20.00]
B = [22.42, 19.39, 20.61, 23.64, 20.61, 23.64, 21.82, 19.39, 23.03, 22.42]


def test_sim_table_example():
    r = paired_comparison(A, B)
    assert r.n == 10
    assert r.mean_diff == pytest.approx(2.363, abs=1e-6)
    assert (r.wins, r.losses, r.ties) == (9, 1, 0)
    # exact two-sided sign test: 2 * (1 + 10) / 1024
    assert r.sign_test_p == pytest.approx(22 / 1024)
    diffs = [y - x for x, y in zip(A, B)]
    mean = sum(diffs) / 10
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / 9)
    half = 2.262 * sd / math.sqrt(10)  # t(df=9)
    assert r.ci95_low == pytest.approx(mean - half)
    assert r.ci95_high == pytest.approx(mean + half)
    assert r.ci95_low < r.mean_diff < r.ci95_high


def test_hand_computed_small():
    r = paired_comparison([0, 0, 0], [1, 2, 3])
    assert r.mean_diff == 2.0
    # sd = 1, t(df=2) = 4.303
    assert r.ci95_low == pytest.approx(2 - 4.303 / math.sqrt(3))
    assert r.ci95_high == pytest.approx(2 + 4.303 / math.sqrt(3))
    assert r.sign_test_p == pytest.approx(0.25)


def test_ties_excluded_and_all_ties():
    r = paired_comparison([1, 2, 3, 4], [1, 3, 3, 5])
    assert (r.wins, r.losses, r.ties) == (2, 0, 2)
    assert r.sign_test_p == pytest.approx(0.5)
    t = paired_comparison([1, 2], [1, 2])
    assert (t.wins, t.losses, t.ties) == (0, 0, 2)
    assert t.sign_test_p == 1.0
    assert t.ci95_low == t.ci95_high == 0.0


def test_balanced_p_capped():
    r = paired_comparison([0, 0], [1, -1])
    assert r.sign_test_p == 1.0


def test_single_pair_ci_equals_mean():
    r = paired_comparison([1.0], [3.0])
    assert r.n == 1
    assert r.ci95_low == r.ci95_high == r.mean_diff == 2.0


def test_large_n_uses_normal_critical_value():
    a = [0.0] * 40
    b = [float(i % 2) for i in range(40)]
    r = paired_comparison(a, b)
    sd = math.sqrt(sum((d - 0.5) ** 2 for d in b) / 39)
    assert r.ci95_high - r.mean_diff == pytest.approx(1.96 * sd / math.sqrt(40))


def test_errors_and_frozen():
    with pytest.raises(ValueError):
        paired_comparison([1], [1, 2])
    with pytest.raises(ValueError):
        paired_comparison([], [])
    with pytest.raises(Exception):
        paired_comparison([1], [2]).n = 5  # type: ignore[misc]
    assert isinstance(paired_comparison([1], [2]), PairedResult)
