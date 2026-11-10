"""Order statistics the board's medians, intervals and stopping rules rest on."""

import math
import unittest

from lka_quiet.stats import (
    geomean,
    median,
    median_ci,
    quantile_nearest_rank,
    relative_halfwidth,
    spread,
)


class Median(unittest.TestCase):
    def test_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_even_is_mean_of_middle_pair(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_single(self):
        self.assertEqual(median([7.5]), 7.5)

    def test_empty_is_an_error(self):
        with self.assertRaises(ValueError):
            median([])

    def test_does_not_reorder_input(self):
        xs = [3, 1, 2]
        median(xs)
        self.assertEqual(xs, [3, 1, 2])


class MedianConfidenceInterval(unittest.TestCase):
    """Binomial order-statistic intervals: P(x_(k) <= m <= x_(n-k+1)) = 1 - 2 P(Bin(n, 1/2) <= k-1)."""

    def test_five_runs_cannot_reach_95_percent(self):
        self.assertIsNone(median_ci([1, 2, 3, 4, 5]))

    def test_six_runs_give_min_max_at_96_9_percent(self):
        lo, hi, coverage = median_ci([6, 1, 5, 2, 4, 3])
        self.assertEqual((lo, hi), (1, 6))
        self.assertAlmostEqual(coverage, 1 - 2 / 64)

    def test_seven_runs_give_min_max_at_98_4_percent(self):
        lo, hi, coverage = median_ci(list(range(1, 8)))
        self.assertEqual((lo, hi), (1, 7))
        self.assertAlmostEqual(coverage, 1 - 2 / 128)

    def test_nine_runs_drop_one_from_each_end(self):
        lo, hi, coverage = median_ci(list(range(1, 10)))
        self.assertEqual((lo, hi), (2, 8))
        self.assertAlmostEqual(coverage, 1 - 2 * 10 / 512)

    def test_fifteen_runs(self):
        lo, hi, coverage = median_ci(list(range(1, 16)))
        self.assertEqual((lo, hi), (4, 12))
        self.assertAlmostEqual(coverage, 1 - 2 * 576 / 32768)

    def test_coverage_never_below_requested(self):
        for n in range(6, 60):
            result = median_ci(list(range(n)))
            self.assertIsNotNone(result)
            self.assertGreaterEqual(result[2], 0.95)

    def test_interval_is_the_tightest_meeting_the_level(self):
        for n in range(6, 60):
            lo, hi, _ = median_ci(list(range(1, n + 1)))
            k = int(lo)
            tighter = 1 - 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n
            self.assertLess(tighter, 0.95, f"n={n}: k={k + 1} would still cover")

    def test_lower_confidence_tightens(self):
        lo95, hi95, _ = median_ci(list(range(1, 21)))
        lo80, hi80, _ = median_ci(list(range(1, 21)), confidence=0.80)
        self.assertGreaterEqual(lo80, lo95)
        self.assertLessEqual(hi80, hi95)
        self.assertLess(hi80 - lo80, hi95 - lo95)


class RelativeHalfwidth(unittest.TestCase):
    def test_identical_runs(self):
        self.assertEqual(relative_halfwidth([100.0] * 7), 0.0)

    def test_one_percent(self):
        self.assertAlmostEqual(relative_halfwidth([99, 100, 100, 100, 100, 100, 101]), 0.01)

    def test_too_few_runs_is_infinite(self):
        self.assertEqual(relative_halfwidth([100.0] * 5), math.inf)


class Spread(unittest.TestCase):
    def test_relative_range(self):
        self.assertAlmostEqual(spread([99, 100, 101]), 0.02)

    def test_single(self):
        self.assertEqual(spread([5.0]), 0.0)


class NearestRankQuantile(unittest.TestCase):
    def test_pinned_ranks(self):
        xs = list(range(1, 101))
        self.assertEqual(quantile_nearest_rank(xs, 0.025), 3)
        self.assertEqual(quantile_nearest_rank(xs, 0.975), 98)
        self.assertEqual(quantile_nearest_rank(xs, 0.0), 1)
        self.assertEqual(quantile_nearest_rank(xs, 1.0), 100)
        self.assertEqual(quantile_nearest_rank(xs, 0.5), 50)

    def test_unsorted_input(self):
        self.assertEqual(quantile_nearest_rank([5, 1, 4, 2, 3], 0.6), 3)


class GeometricMean(unittest.TestCase):
    def test_value(self):
        self.assertAlmostEqual(geomean([1, 4]), 2.0)
        self.assertAlmostEqual(geomean([2, 8, 4]), 4.0)

    def test_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            geomean([1, 0])


if __name__ == "__main__":
    unittest.main()
