"""Offline tests for the monitor's pure helpers (stdlib only, no RPC).

The sizing math is load-bearing: repaidShares and seizedAssets are what the executor puts
on-chain, and the Sushi quote is built for exactly `seized_assets`. These are checked against
the Morpho.sol liquidate formulas.
"""
import unittest

from analysis.models import ORACLE_PRICE_SCALE, WAD, lif_from_lltv
from analysis.monitor import (assess, debt_usd, decode_market_params, decode_position,
                              size_liquidation)
from analysis.protocols import TOKENS


def _word(x: int) -> str:
    return hex(x)[2:].rjust(64, "0")


class TestDecoders(unittest.TestCase):
    def test_decode_position(self):
        ret = "0x" + _word(111) + _word(222) + _word(333)
        self.assertEqual(decode_position(ret),
                         {"supply_shares": 111, "borrow_shares": 222, "collateral": 333})

    def test_decode_market_params(self):
        loan = 0x1111111111111111111111111111111111111111
        coll = 0x2222222222222222222222222222222222222222
        ret = "0x" + _word(loan) + _word(coll) + _word(0) + _word(0) + _word(int(0.86e18))
        d = decode_market_params(ret)
        self.assertEqual(d["loan"], "0x" + "11" * 20)
        self.assertEqual(d["collateral"], "0x" + "22" * 20)
        self.assertEqual(d["lltv"], int(0.86e18))


class TestSizing(unittest.TestCase):
    def test_size_liquidation_capped_by_debt(self):
        # collateral generously covers debt -> repaid == full debt
        lltv = int(0.86e18)
        price = 2 * ORACLE_PRICE_SCALE          # 1 coll = 2 loan
        collateral = 100 * WAD                  # worth 200 loan
        debt = 170 * WAD
        s = size_liquidation(debt, collateral, price, lltv)
        self.assertEqual(s["repaid_assets"], debt)  # debt < collValue/lif -> full close
        # seized = repaid * lif / price
        lif = lif_from_lltv(lltv)
        exp_seized = int(int(debt * lif) * ORACLE_PRICE_SCALE // price)
        self.assertEqual(s["seized_assets"], min(exp_seized, collateral))

    def test_size_liquidation_capped_by_collateral(self):
        # tiny collateral, huge debt -> repaid capped by collateral value / lif; seized<=collateral
        lltv = int(0.86e18)
        price = 2 * ORACLE_PRICE_SCALE
        collateral = 10 * WAD                   # worth 20 loan
        debt = 1000 * WAD
        s = size_liquidation(debt, collateral, price, lltv)
        lif = lif_from_lltv(lltv)
        coll_value = collateral * price // ORACLE_PRICE_SCALE   # 20 loan
        self.assertEqual(s["repaid_assets"], int(coll_value / lif))
        self.assertLessEqual(s["seized_assets"], collateral)

    def test_seized_never_exceeds_collateral(self):
        lltv = int(0.915e18)
        price = ORACLE_PRICE_SCALE               # 1:1
        collateral = 5 * WAD
        debt = 100 * WAD
        s = size_liquidation(debt, collateral, price, lltv)
        self.assertLessEqual(s["seized_assets"], collateral)


class TestHF(unittest.TestCase):
    def test_hf_at_threshold(self):
        # collateral*price/1e36*lltv == debt -> HF == 1
        lltv = int(0.86e18)
        price = 2 * ORACLE_PRICE_SCALE
        collateral = 100 * WAD
        # max borrow = 100*2*0.86 = 172
        pos = {"supply_shares": 0, "borrow_shares": 172 * WAD, "collateral": collateral}
        mkt = {"total_borrow_assets": 172 * WAD, "total_borrow_shares": 172 * WAD}
        a = assess(pos, mkt, price, lltv)
        self.assertAlmostEqual(a["hf"], 1.0, places=4)


class TestDebtUsd(unittest.TestCase):
    def test_stable_debt_is_usd(self):
        usdc = TOKENS["vbUSDC"]["address"]
        self.assertAlmostEqual(debt_usd(usdc, 1_500_000000), 1500.0, places=6)  # 6 dec

    def test_nonstable_debt_is_none(self):
        self.assertIsNone(debt_usd(TOKENS["vbETH"]["address"], 10 ** 18))


if __name__ == "__main__":
    unittest.main()
