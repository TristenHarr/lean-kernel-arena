"""The board's random numbers: one generator, the same stream on every machine and Python version."""

import hashlib
import unittest

from lka_quiet.rng import SplitMix64, seed_from


class SplitMix64Stream(unittest.TestCase):
    def test_reference_stream_from_seed_zero(self):
        # Vigna's reference SplitMix64 outputs for state 0
        rng = SplitMix64(0)
        self.assertEqual(rng.next_u64(), 0xE220A8397B1DCDAF)
        self.assertEqual(rng.next_u64(), 0x6E789E6AA1B965F4)
        self.assertEqual(rng.next_u64(), 0x06C45D188009454F)

    def test_outputs_stay_in_64_bits(self):
        rng = SplitMix64(2**64 - 1)
        for _ in range(1000):
            value = rng.next_u64()
            self.assertGreaterEqual(value, 0)
            self.assertLess(value, 2**64)

    def test_seed_is_reduced_modulo_two_to_the_64(self):
        self.assertEqual(SplitMix64(2**64 + 5).next_u64(), SplitMix64(5).next_u64())


class Below(unittest.TestCase):
    def test_range(self):
        rng = SplitMix64(7)
        for n in (1, 2, 3, 7, 1000, 2**63 + 3):
            for _ in range(200):
                self.assertTrue(0 <= rng.below(n) < n)

    def test_one_is_always_zero(self):
        rng = SplitMix64(11)
        self.assertEqual({rng.below(1) for _ in range(50)}, {0})

    def test_rejects_non_positive_bounds(self):
        with self.assertRaises(ValueError):
            SplitMix64(1).below(0)

    def test_uniform_within_chi_square_bound(self):
        rng = SplitMix64(12345)
        n, draws = 10, 100_000
        counts = [0] * n
        for _ in range(draws):
            counts[rng.below(n)] += 1
        expected = draws / n
        chi2 = sum((c - expected) ** 2 / expected for c in counts)
        # 99.9th percentile of chi-square with 9 degrees of freedom
        self.assertLess(chi2, 27.88)

    def test_large_bound_uses_rejection_not_modulo(self):
        # With n just above 2^63, plain modulo would put two thirds of the mass below 2^63 - 1;
        # rejection sampling keeps each half equally likely.
        rng = SplitMix64(99)
        n = 2**63 + 2**62
        low = sum(1 for _ in range(20_000) if rng.below(n) < n // 2)
        self.assertAlmostEqual(low / 20_000, 0.5, delta=0.02)


class Shuffle(unittest.TestCase):
    def test_is_a_permutation(self):
        items = list(range(100))
        shuffled = SplitMix64(3).shuffled(items)
        self.assertEqual(sorted(shuffled), items)
        self.assertNotEqual(shuffled, items)
        self.assertEqual(items, list(range(100)), "the input is not modified")

    def test_same_seed_same_order(self):
        self.assertEqual(SplitMix64(42).shuffled(range(50)), SplitMix64(42).shuffled(range(50)))

    def test_different_seed_different_order(self):
        self.assertNotEqual(SplitMix64(42).shuffled(range(50)), SplitMix64(43).shuffled(range(50)))

    def test_every_position_reachable(self):
        firsts = {SplitMix64(seed).shuffled(range(5))[0] for seed in range(200)}
        self.assertEqual(firsts, set(range(5)))


class SeedFrom(unittest.TestCase):
    def test_is_the_first_eight_bytes_of_sha256_over_unit_separated_parts(self):
        digest = hashlib.sha256("round-1\x1fpass-0".encode()).digest()
        self.assertEqual(seed_from("round-1", "pass-0"), int.from_bytes(digest[:8], "big"))

    def test_parts_are_separated(self):
        self.assertNotEqual(seed_from("ab", "c"), seed_from("a", "bc"))

    def test_order_matters(self):
        self.assertNotEqual(seed_from("a", "b"), seed_from("b", "a"))


if __name__ == "__main__":
    unittest.main()
