"""Statistics tests.

These check arithmetic, determinism and the pairing logic -- not the behaviour of
the system under test. They matter because a wrong confidence interval is
invisible: it looks like a number, and it will be quoted.

Wherever a value can be derived by hand, the test uses the hand derivation
rather than a second implementation, so the test cannot agree with a bug by
sharing it.
"""

from __future__ import annotations

import pytest

from arcgis_agent_lab.stats import (
    bootstrap_ci,
    mcnemar_exact,
    paired_bootstrap_difference,
)


class TestBootstrapCI:
    def test_is_deterministic_for_a_fixed_seed(self) -> None:
        """An interval that moves between runs is not a property of the data."""
        values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0]
        assert bootstrap_ci(values, seed=7) == bootstrap_ci(values, seed=7)

    def test_interval_brackets_the_point_estimate(self) -> None:
        ci = bootstrap_ci([1.0] * 7 + [0.0] * 3)
        assert ci.point == pytest.approx(0.7)
        assert ci.low <= ci.point <= ci.high

    def test_constant_sample_yields_zero_width(self) -> None:
        ci = bootstrap_ci([1.0] * 10)
        assert ci.width == pytest.approx(0.0)

    def test_smaller_sample_yields_wider_interval(self) -> None:
        """The point of reporting intervals: resolution depends on n."""
        small = bootstrap_ci([1.0, 0.0, 1.0, 0.0, 1.0])
        large = bootstrap_ci([1.0, 0.0] * 25)
        assert small.width > large.width

    def test_rejects_empty_input(self) -> None:
        with pytest.raises(ValueError):
            bootstrap_ci([])

    def test_rejects_invalid_confidence(self) -> None:
        with pytest.raises(ValueError):
            bootstrap_ci([1.0, 0.0], confidence=1.5)


class TestMcNemar:
    def test_matches_hand_computed_binomial(self) -> None:
        """5 discordant pairs one way, 1 the other -> Binomial(6, 0.5), k=1.

        Two-sided exact p by the "probability <= observed" rule:
        P(0)+P(1)+P(5)+P(6) = (1+6+6+1)/64 = 14/64 = 0.21875.
        """
        a = [True] * 5 + [False] + [True] * 4
        b = [False] * 5 + [True] + [True] * 4
        result = mcnemar_exact(a, b)
        assert result.a_only == 5
        assert result.b_only == 1
        assert result.discordant == 6
        assert result.p_value == pytest.approx(14 / 64, abs=1e-12)

    def test_no_disagreement_is_uninformative(self) -> None:
        """Identical configurations carry no evidence either way."""
        a = [True, False, True]
        b = [True, False, True]
        result = mcnemar_exact(a, b)
        assert not result.informative
        assert result.p_value == pytest.approx(1.0)
        assert result.both_pass == 2 and result.both_fail == 1

    def test_large_consistent_gap_is_significant(self) -> None:
        a = [True] * 20 + [False] * 5
        b = [False] * 20 + [True] * 5
        result = mcnemar_exact(a, b)
        assert result.a_only == 20 and result.b_only == 5
        assert result.p_value < 0.01

    def test_requires_equal_length(self) -> None:
        with pytest.raises(ValueError):
            mcnemar_exact([True], [True, False])


class TestPairedBootstrap:
    def test_detects_a_consistent_difference(self) -> None:
        diff = paired_bootstrap_difference([1.0] * 20, [0.0] * 20)
        assert diff.point == pytest.approx(1.0)
        assert diff.excludes_zero

    def test_is_antisymmetric_under_swap(self) -> None:
        """The pairing must survive the swap: intervals mirror, point flips.

        A non-paired implementation would fail this -- resampling the two sides
        independently does not give ``low == -high_swapped``.
        """
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [0.0, 1.0, 1.0, 2.0, 5.0]
        forward = paired_bootstrap_difference(a, b, seed=3)
        backward = paired_bootstrap_difference(b, a, seed=3)
        assert forward.point == pytest.approx(-backward.point)
        assert forward.low == pytest.approx(-backward.high)
        assert forward.high == pytest.approx(-backward.low)

    def test_identical_samples_do_not_exclude_zero(self) -> None:
        values = [1.0, 2.0, 3.0]
        diff = paired_bootstrap_difference(values, list(values))
        assert diff.point == pytest.approx(0.0)
        assert not diff.excludes_zero

    def test_is_deterministic_for_a_fixed_seed(self) -> None:
        a = [1.0, 0.0, 1.0, 1.0]
        b = [0.0, 0.0, 1.0, 0.0]
        assert paired_bootstrap_difference(a, b, seed=11) == paired_bootstrap_difference(
            a, b, seed=11
        )

    def test_requires_equal_length(self) -> None:
        with pytest.raises(ValueError):
            paired_bootstrap_difference([1.0, 2.0], [1.0])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
