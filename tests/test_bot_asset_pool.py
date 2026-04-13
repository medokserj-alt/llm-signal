import unittest

import get_signal_json


class TestBotAssetPool(unittest.TestCase):
    def test_pool_snapshot_uses_only_fixed_five_asset_universe(self) -> None:
        original = get_signal_json.get_pair_ticker
        get_signal_json.get_pair_ticker = lambda symbol: {"last": 1.0, "change": 0.0, "symbol": symbol}
        try:
            snapshot = get_signal_json.get_pool_snapshot()
        finally:
            get_signal_json.get_pair_ticker = original

        self.assertEqual(
            list(snapshot.keys()),
            ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"],
        )


if __name__ == "__main__":
    unittest.main()
