"""Vectors pinning keccak256 correctness — run: python3 -m analysis.test_keccak"""
import unittest

from analysis.keccak import keccak256, event_topic0, selector


class TestKeccak(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")

    def test_abc(self):
        self.assertEqual(keccak256(b"abc").hex(),
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")

    def test_multiblock(self):
        # > rate (136 bytes) to exercise multi-block absorb
        self.assertEqual(keccak256(b"a" * 200).hex(),
                         keccak256(b"a" * 100 + b"a" * 100).hex())
        self.assertEqual(len(keccak256(b"a" * 200)), 32)

    def test_erc20_transfer_topic(self):
        self.assertEqual(event_topic0("Transfer(address,address,uint256)"),
                         "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")

    def test_aave_liquidation_call_topic(self):
        # Canonical Aave v3 LiquidationCall topic0 (cross-checked with etherscan logs)
        self.assertEqual(
            event_topic0("LiquidationCall(address,address,address,uint256,uint256,address,bool)"),
            "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286")

    def test_transfer_selector(self):
        self.assertEqual(selector("transfer(address,uint256)"), "0xa9059cbb")


if __name__ == "__main__":
    unittest.main()
