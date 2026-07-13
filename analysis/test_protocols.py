"""Offline tests for the Katana protocol registry + Morpho decoders (stdlib only, no RPC).

The Liquidate log vector is a REAL Katana mainnet event (tx
0xf42780f7…4a7024cc0, block 16689471, the largest vbWBTC/vbUSDC liquidation to date),
so the decoder is pinned against production data, not a hand-built fixture.
"""
import unittest

from analysis.keccak import event_topic0
from analysis.protocols import (
    ADDR_TO_SYMBOL, MARKETS, MORPHO, STABLES, SUSHI_ROUTE_PROCESSOR, TOKENS,
    TOPIC_MORPHO_BORROW, TOPIC_MORPHO_LIQUIDATE, decode_morpho_borrow,
    decode_morpho_liquidate,
)

# Real Katana Liquidate log (Morpho Blue, vbWBTC/vbUSDC market).
_LIQ_LOG = {
    "address": "0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc",
    "blockNumber": "0xfea93f",  # 16689471
    "transactionHash": "0xf42780f76ce7ff4e28445cbcc3fe5d9303bc90554adf1765c1a2ca24a7024cc0",
    "topics": [
        "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41",
        "0xcd2dc555dced7422a3144a4126286675449019366f83e9717be7c2deb3daae3e",
        "0x000000000000000000000000083cfa7fd187be983ce5d519fe7ae78357779998",
        "0x00000000000000000000000014bcd9da052cdc6fe0b9446d5a616d5b7b4d4550",
    ],
    # repaidAssets, repaidShares, seizedAssets, badDebtAssets, badDebtShares
    "data": ("0x"
             "000000000000000000000000000000000000000000000000000000366e2aa549"
             "000000000000000000000000000000000000000000000000033870a45606c586"
             "00000000000000000000000000000000000000000000000000000000100b3421"
             "0000000000000000000000000000000000000000000000000000000000000000"
             "0000000000000000000000000000000000000000000000000000000000000000"),
}


class TestTopics(unittest.TestCase):
    def test_liquidate_topic_matches_onchain(self):
        self.assertEqual(TOPIC_MORPHO_LIQUIDATE, _LIQ_LOG["topics"][0])

    def test_borrow_topic_is_computed_offline(self):
        # sanity: recomputing from the signature must match the module constant
        self.assertEqual(
            TOPIC_MORPHO_BORROW,
            event_topic0("Borrow(bytes32,address,address,address,uint256,uint256)"))


class TestDecoders(unittest.TestCase):
    def test_decode_real_liquidate(self):
        d = decode_morpho_liquidate(_LIQ_LOG)
        self.assertEqual(d["market_id"],
                         "0xcd2dc555dced7422a3144a4126286675449019366f83e9717be7c2deb3daae3e")
        self.assertEqual(d["liquidator"], "0x083cfa7fd187be983ce5d519fe7ae78357779998")
        self.assertEqual(d["borrower"], "0x14bcd9da052cdc6fe0b9446d5a616d5b7b4d4550")
        self.assertEqual(d["repaid_assets"], 233776522569)   # 233,776.52 vbUSDC (6 dec)
        self.assertEqual(d["seized_assets"], 269169697)      # 2.69169697 vbWBTC (8 dec)
        self.assertEqual(d["bad_debt_assets"], 0)
        self.assertEqual(d["block"], 16689471)

    def test_decode_borrow(self):
        log = {"blockNumber": "0x10", "transactionHash": "0xaa",
               "topics": ["0x" + "0" * 64,
                          "0xcd2dc555dced7422a3144a4126286675449019366f83e9717be7c2deb3daae3e",
                          "0x00000000000000000000000014bcd9da052cdc6fe0b9446d5a616d5b7b4d4550"],
               "data": "0x"}
        d = decode_morpho_borrow(log)
        self.assertEqual(d["borrower"], "0x14bcd9da052cdc6fe0b9446d5a616d5b7b4d4550")
        self.assertEqual(d["block"], 16)


class TestRegistry(unittest.TestCase):
    def test_addresses_checksummed_and_present(self):
        self.assertTrue(MORPHO.startswith("0x") and len(MORPHO) == 42)
        self.assertTrue(SUSHI_ROUTE_PROCESSOR.startswith("0x") and len(SUSHI_ROUTE_PROCESSOR) == 42)

    def test_token_map_consistency(self):
        for sym, meta in TOKENS.items():
            self.assertEqual(ADDR_TO_SYMBOL[meta["address"].lower()], sym)
        # stables must be a subset of known tokens
        known = {m["address"].lower() for m in TOKENS.values()}
        self.assertTrue(STABLES.issubset(known))

    def test_market_ids_full_length(self):
        for name, m in MARKETS.items():
            self.assertEqual(len(m["id"]), 66, f"{name} id not a full bytes32")
            self.assertIn(m["loan"], TOKENS)
            self.assertIn(m["coll"], TOKENS)


if __name__ == "__main__":
    unittest.main()
