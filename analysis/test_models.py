"""Unit tests for pure Gate-1 math — run: python3 -m analysis.test_models"""
import unittest

from analysis.models import (WAD, accrued_interest, lif_from_lltv, morpho_bonus_usd,
                             gross_margin_usd, gas_cost_usd, top_share, hhi, month_key,
                             day_ts, per_month_usd, w_taylor_compounded)


class TestLif(unittest.TestCase):
    def test_known_values(self):
        # lltv=0.945 -> 1/(0.3*0.945+0.7) = 1.0167768 (same formula as base reference pnl.py)
        self.assertAlmostEqual(lif_from_lltv(int(0.945 * WAD)), 1.0167768, places=6)
        # lltv=0.77 -> 1/(0.231+0.7) = 1.07411...
        self.assertAlmostEqual(lif_from_lltv(int(0.77 * WAD)), 1.0741138, places=6)

    def test_max_cap(self):
        # very low lltv caps at 1.15
        self.assertEqual(lif_from_lltv(int(0.01 * WAD)), 1.15)

    def test_bonus(self):
        self.assertAlmostEqual(morpho_bonus_usd(10_000, int(0.77 * WAD)),
                               10_000 * 0.0741138, delta=0.5)


class TestMargins(unittest.TestCase):
    def test_gross(self):
        self.assertEqual(gross_margin_usd(105.0, 100.0), 5.0)

    def test_gas(self):
        # 500k gas at 50 gwei, native at $0.02
        self.assertAlmostEqual(gas_cost_usd(500_000, 50 * 10 ** 9, 0.02), 0.0005)


class TestConcentration(unittest.TestCase):
    def test_top_share(self):
        c = {"a": 60, "b": 30, "c": 10}
        self.assertAlmostEqual(top_share(c, 1), 0.6)
        self.assertAlmostEqual(top_share(c, 2), 0.9)
        self.assertEqual(top_share({}, 3), 0.0)

    def test_hhi(self):
        self.assertAlmostEqual(hhi({"a": 1}), 1.0)
        self.assertAlmostEqual(hhi({"a": 50, "b": 50}), 0.5)
        self.assertEqual(hhi({}), 0.0)


class TestTime(unittest.TestCase):
    def test_month_key(self):
        self.assertEqual(month_key(1783147661), "2026-07")  # 2026-07-04 UTC

    def test_day_ts(self):
        self.assertEqual(day_ts(1783147661) % 86400, 0)
        self.assertLessEqual(day_ts(1783147661), 1783147661)

    def test_per_month(self):
        evs = [{"ts": 1783147661, "usd": 10.0}, {"ts": 1783147661, "usd": 5.0},
               {"ts": 1780000000, "usd": 1.0}, {"ts": 1780000000, "usd": None}]
        self.assertEqual(per_month_usd(evs, "usd"),
                         {"2026-07": 15.0, "2026-05": 1.0})


class TestAccrual(unittest.TestCase):
    """Morpho MathLib.wTaylorCompounded / _accrueInterest port (review C3). The rate is
    PER-SECOND WAD — a x1000 dimensional slip would show up instantly in these vectors."""

    def test_taylor_matches_exp_minus_one(self):
        import math
        rate = int(0.05 * WAD) // 31_536_000        # 5% APR as a per-second WAD rate
        for elapsed in (1, 3600, 86400, 30 * 86400):
            got = w_taylor_compounded(rate, elapsed) / WAD
            exact = math.exp(rate / WAD * elapsed) - 1.0
            self.assertAlmostEqual(got, exact, delta=exact * 1e-4 + 1e-12)

    def test_taylor_exact_terms(self):
        # hand-computed: rate*n = 0.1 WAD -> 0.1 + 0.005 + 0.000166.. (floor each term)
        rate, n = WAD // 100, 10                    # rate*n = 0.1 WAD
        first = rate * n
        second = first * first // (2 * WAD)
        third = second * first // (3 * WAD)
        self.assertEqual(w_taylor_compounded(rate, n), first + second + third)

    def test_accrued_interest_magnitude(self):
        # $1M-scale totals at 5% APR for 1 day ≈ 0.0137% — the exact stale-HF gap C3 is about
        rate = int(0.05 * WAD) // 31_536_000
        total = 10 ** 24
        got = accrued_interest(total, rate, 86400)
        self.assertAlmostEqual(got / total, 0.05 / 365, delta=0.05 / 365 * 0.01)

    def test_accrued_interest_zero_cases(self):
        self.assertEqual(accrued_interest(10 ** 24, 0, 86400), 0)
        self.assertEqual(accrued_interest(10 ** 24, 10 ** 9, 0), 0)
        self.assertEqual(accrued_interest(10 ** 24, 10 ** 9, -5), 0)


if __name__ == "__main__":
    unittest.main()
