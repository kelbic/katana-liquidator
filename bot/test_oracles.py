"""Offline tests for the oracle-tx fingerprint config (no network).

The address set is the mempool same-block layer's ONLY link between a pending tx and 'which
market is about to reprice' — a wrong/loose match either misses a window or arms the wrong
market, so aggregator/transmitter mapping and tip extraction are load-bearing."""
import os
import unittest

os.environ.setdefault("DRY_RUN", "1")

from analysis.protocols import MARKETS               # noqa: E402
from bot import oracles as oc                         # noqa: E402

BTC_AGG = "0x56ac2b1b78225d47993e8866795a34ad540a515c"
ETH_AGG = "0x47522e7273344f1016a1e67e496ddb4f77d852c9"
LBTC_AGG = "0xa3e7cf38e05f6ed4e9c96a477263c984e2e30326"
REDSTONE = "0xe8d9fbc10e00ecc9f0694617075fdaf657a76fb2"
# 0x9185… signs BOTH BTC/USD and ETH/USD rounds (shared committee -> ambiguous by `from`)
SHARED_EOA = "0x9185d7aeabaa7a7edad2954536e1536bfde2f7e8"
# 0x7f33… signs ONLY BTC/USD
BTC_ONLY_EOA = "0x7f333870a01566fac9b7207c0abd096c761914d6"

WBTC_USDC = oc._lc(MARKETS["vbWBTC/vbUSDC"]["id"])
WBTC_USDT = oc._lc(MARKETS["vbWBTC/vbUSDT"]["id"])
ETH_USDC = oc._lc(MARKETS["vbETH/vbUSDC"]["id"])
ETH_USDT = oc._lc(MARKETS["vbETH/vbUSDT"]["id"])
LBTC_USDC = oc._lc(MARKETS["LBTC/vbUSDC"]["id"])
WEETH_ETH = oc._lc(MARKETS["weETH/vbETH"]["id"])


class TestReverseIndexes(unittest.TestCase):
    def test_every_market_has_feeds(self):
        # all six live markets are covered
        self.assertEqual(len(oc.MARKET_FEEDS), 6)
        for mid in (WBTC_USDC, WBTC_USDT, ETH_USDC, ETH_USDT, LBTC_USDC, WEETH_ETH):
            self.assertIn(mid, oc.MARKET_FEEDS)

    def test_aggregators_map_to_feeds(self):
        self.assertEqual(oc.AGG_TO_FEED[BTC_AGG], "BTC/USD")
        self.assertEqual(oc.AGG_TO_FEED[ETH_AGG], "ETH/USD")
        self.assertEqual(oc.AGG_TO_FEED[REDSTONE], "weETH_FUNDAMENTAL")

    def test_transmitter_committee_is_shared(self):
        self.assertEqual(oc.TRANSMITTER_FEEDS[SHARED_EOA], {"BTC/USD", "ETH/USD"})
        self.assertEqual(oc.TRANSMITTER_FEEDS[BTC_ONLY_EOA], {"BTC/USD"})

    def test_oracle_addrs_membership(self):
        self.assertIn(BTC_AGG, oc.ORACLE_ADDRS)
        self.assertIn(SHARED_EOA, oc.ORACLE_ADDRS)
        self.assertNotIn("0x" + "de" * 20, oc.ORACLE_ADDRS)


class TestMarketResolution(unittest.TestCase):
    def test_aggregator_to_match_is_unambiguous(self):
        # BTC/USD aggregator -> exactly the two WBTC markets
        self.assertEqual(oc.markets_for_tx(to=BTC_AGG, frm=None), {WBTC_USDC, WBTC_USDT})
        self.assertEqual(oc.markets_for_tx(to=ETH_AGG, frm=None), {ETH_USDC, ETH_USDT})
        self.assertEqual(oc.markets_for_tx(to=LBTC_AGG, frm=None), {LBTC_USDC})
        self.assertEqual(oc.markets_for_tx(to=REDSTONE, frm=None), {WEETH_ETH})

    def test_shared_transmitter_from_is_broad(self):
        # 0x9185 signs BTC+ETH -> union of all four BTC/ETH markets (graceful degradation)
        self.assertEqual(oc.markets_for_tx(to=None, frm=SHARED_EOA),
                         {WBTC_USDC, WBTC_USDT, ETH_USDC, ETH_USDT})

    def test_dedicated_transmitter_from_is_narrow(self):
        self.assertEqual(oc.markets_for_tx(to=None, frm=BTC_ONLY_EOA), {WBTC_USDC, WBTC_USDT})

    def test_case_insensitive(self):
        self.assertEqual(oc.markets_for_tx(to=BTC_AGG.upper(), frm=None), {WBTC_USDC, WBTC_USDT})

    def test_unknown_tx_matches_nothing(self):
        self.assertEqual(oc.markets_for_tx(to="0x" + "de" * 20, frm="0x" + "ad" * 20), set())
        self.assertEqual(oc.markets_for_tx(to=None, frm=None), set())
        self.assertFalse(oc.is_oracle_tx("0x" + "de" * 20, "0x" + "ad" * 20))
        self.assertTrue(oc.is_oracle_tx(BTC_AGG, None))
        self.assertTrue(oc.is_oracle_tx(None, SHARED_EOA))

    def test_forwarder_to_still_caught_via_from(self):
        # the on-chain `to` is often an OCR forwarder we don't enumerate; the transmitter `from`
        # is the reliable fingerprint, and it still resolves the market set
        self.assertEqual(oc.markets_for_tx(to="0x120e60168cde6094b0d9d3306688334b58817750",
                                           frm=BTC_ONLY_EOA), {WBTC_USDC, WBTC_USDT})

    def test_market_pair_label(self):
        self.assertEqual(oc.market_pair(WBTC_USDC), "vbWBTC/vbUSDC")
        self.assertEqual(oc.market_pair("0x" + "ab" * 32), ("0x" + "ab" * 32)[:10])


class TestTipExtraction(unittest.TestCase):
    def test_type2_maxpriority_direct(self):
        tx = {"type": "0x2", "maxPriorityFeePerGas": hex(3376432),
              "maxFeePerGas": hex(4776432)}
        self.assertEqual(oc.tx_priority_fee_wei(tx), 3376432)      # committee's tip, to the wei

    def test_type2_effective_capped_by_maxfee_minus_base(self):
        # when maxFee - base < maxPriority, the effective tip is the smaller (op-reth ordering)
        tx = {"maxPriorityFeePerGas": hex(5_000_000), "maxFeePerGas": hex(1_600_000)}
        base = 1_000_000                                          # 0.001 gwei
        self.assertEqual(oc.tx_priority_fee_wei(tx, base), 600_000)

    def test_type2_maxpriority_when_below_maxfee_headroom(self):
        tx = {"maxPriorityFeePerGas": hex(3376432), "maxFeePerGas": hex(4776432)}
        self.assertEqual(oc.tx_priority_fee_wei(tx, 1_000_000), 3376432)

    def test_legacy_gasprice_minus_base(self):
        tx = {"gasPrice": hex(1_500_000)}
        self.assertEqual(oc.tx_priority_fee_wei(tx, 1_000_000), 500_000)
        self.assertEqual(oc.tx_priority_fee_wei(tx), 1_500_000)   # no base -> raw gasPrice

    def test_accepts_int_fields(self):
        self.assertEqual(oc.tx_priority_fee_wei({"maxPriorityFeePerGas": 42}), 42)

    def test_unparseable_returns_none(self):
        self.assertIsNone(oc.tx_priority_fee_wei({}))
        self.assertIsNone(oc.tx_priority_fee_wei({"maxPriorityFeePerGas": "notahex"}))

    def test_never_negative(self):
        tx = {"gasPrice": hex(500_000)}
        self.assertEqual(oc.tx_priority_fee_wei(tx, 1_000_000), 0)   # base above gasPrice -> 0


if __name__ == "__main__":
    unittest.main()
